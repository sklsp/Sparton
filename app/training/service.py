"""Leonidas — LoRA training orchestration via the Ostris AI Toolkit.

Adapted from Apollo `lora_training_service.py`: config building, subprocess
management, and progress parsing are kept; run history now lives in SQL
(``TrainingRun`` rows) instead of per-project JSON files.
"""

from __future__ import annotations

import atexit
import logging
import re
import subprocess  # noqa: S404 - fixed argv, no shell
import threading
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.training.hardware import detect_hardware
from app.training.preflight import advise

logger = logging.getLogger(__name__)

DEFAULT_ARCH = "sdxl"

ARCH_DEFAULTS: dict[str, dict[str, Any]] = {
    "sdxl": {
        "base_model": "stabilityai/stable-diffusion-xl-base-1.0",
        "resolution": [1024, 1024],
        "dtype": "bf16",
        "noise_scheduler": "ddpm",
    },
    "flux": {
        "base_model": "black-forest-labs/FLUX.1-dev",
        "resolution": [1024, 1024],
        "dtype": "bf16",
        "quantize": True,
    },
}

TRAINING_PRESETS: dict[str, dict[str, Any]] = {
    "character": {"steps": 2000, "learning_rate": 1e-4, "lora_rank": 32, "batch_size": 1},
    "style": {"steps": 3000, "learning_rate": 1e-4, "lora_rank": 16, "batch_size": 2},
    "product": {"steps": 1500, "learning_rate": 2e-4, "lora_rank": 16, "batch_size": 1},
    "concept": {"steps": 2500, "learning_rate": 1e-4, "lora_rank": 24, "batch_size": 1},
    "general": {"steps": 2000, "learning_rate": 1e-4, "lora_rank": 16, "batch_size": 1},
}


class TrainingError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def build_training_config(
    *,
    project_name: str,
    dataset_path: str,
    output_path: str,
    arch: str = DEFAULT_ARCH,
    base_model: str | None = None,
    trigger_word: str = "",
    steps: int = 2000,
    learning_rate: float = 1e-4,
    batch_size: int = 1,
    resolution: list[int] | None = None,
    lora_rank: int = 16,
    lora_alpha: int | None = None,
    save_every: int = 250,
    max_saves_to_keep: int = 4,
    sample_every: int = 0,
    sample_prompts: list[str] | None = None,
    device: str = "cuda:0",
) -> dict[str, Any]:
    """Build an AI Toolkit (v0.12.x schema) training config."""
    arch_defaults = ARCH_DEFAULTS.get(arch, ARCH_DEFAULTS[DEFAULT_ARCH])
    base_model = base_model or arch_defaults["base_model"]
    resolution = resolution or list(arch_defaults["resolution"])
    lora_alpha = lora_alpha if lora_alpha is not None else lora_rank

    config: dict[str, Any] = {
        "config": {
            "name": project_name,
            "process": [
                {
                    "type": "sd_trainer",
                    "training_folder": str(Path(output_path).parent),
                    "device": device,
                    "trigger_word": trigger_word,
                    "network": {
                        "type": "lora",
                        "linear": lora_rank,
                        "linear_alpha": lora_alpha,
                    },
                    "save": {
                        "dtype": "float16",
                        "save_every": save_every,
                        "max_step_saves_to_keep": max_saves_to_keep,
                    },
                    "datasets": [
                        {
                            "folder_path": str(dataset_path),
                            "caption_ext": "txt",
                            "caption_dropout_rate": 0.05,
                            "shuffle_tokens": False,
                            "cache_latents_to_disk": True,
                            "resolution": resolution,
                        }
                    ],
                    "train": {
                        "batch_size": batch_size,
                        "steps": steps,
                        "gradient_accumulation_steps": 1,
                        "train_unet": True,
                        "train_text_encoder": False,
                        "noise_scheduler": arch_defaults.get("noise_scheduler", "ddpm"),
                        "optimizer": "adamw8bit",
                        "lr": learning_rate,
                        "dtype": arch_defaults.get("dtype", "bf16"),
                    },
                    "model": {
                        "name_or_path": base_model,
                        "is_flux": arch == "flux",
                        "quantize": arch_defaults.get("quantize", False),
                    },
                }
            ],
        }
    }

    if sample_every > 0 and sample_prompts:
        config["config"]["process"][0]["sample"] = {
            "sampling_every": sample_every,
            "guidance_scale": 4.0,
            "sample_steps": 20,
            "prompts": sample_prompts,
        }
    else:
        config["config"]["process"][0]["disable_sampling"] = True

    return config


class AIToolkitProcess:
    """Owns exactly one trainer subprocess + its parsed log tail."""

    _STEP_RE = re.compile(r"(\d+)/(\d+)")
    _LOSS_RE = re.compile(r"loss[:=]\s*([0-9.]+)")

    def __init__(self, python_exe: str, toolkit_path: Path, config_path: Path, log_path: Path) -> None:
        self.python_exe = python_exe
        self.toolkit_path = toolkit_path
        self.config_path = config_path
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self.current_step = 0
        self.total_steps = 0
        self.loss: float | None = None
        self.last_line = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        creationflags = 0x00000200 if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv
            [self.python_exe, "run.py", str(self.config_path)],
            cwd=str(self.toolkit_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        self._thread = threading.Thread(target=self.stream, daemon=True)
        self._thread.start()
        logger.info("[TRAIN] Started AI Toolkit process pid=%s", self.process.pid)

    def stream(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        with self.log_path.open("w", encoding="utf-8") as log_file:
            for line in self.process.stdout:
                log_file.write(line)
                log_file.flush()
                self._parse(line)

    def _parse(self, line: str) -> None:
        with self._lock:
            self.last_line = line.strip()[:200]
            match = self._STEP_RE.search(line)
            if match:
                self.current_step = int(match.group(1))
                self.total_steps = int(match.group(2))
            loss_match = self._LOSS_RE.search(line)
            if loss_match:
                try:
                    self.loss = float(loss_match.group(1))
                except ValueError:
                    pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current_step": self.current_step,
                "total_steps": self.total_steps,
                "loss": self.loss,
                "last_line": self.last_line,
            }

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self, timeout: float = 15.0) -> bool:
        if self.process is None or self.process.poll() is not None:
            return True
        try:
            import os

            if os.name == "nt":
                subprocess.run(  # noqa: S603
                    ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                    capture_output=True, check=False, timeout=timeout,
                )
            else:
                self.process.terminate()
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            return True
        except OSError as exc:
            logger.warning("[TRAIN] Failed to stop process: %s", exc)
            return False


_REGISTRY: set[AIToolkitProcess] = set()


@atexit.register
def _stop_all_trainers() -> None:
    for proc in list(_REGISTRY):
        proc.stop()


class LoRATrainingService:
    """Preflight, configure, launch, monitor, and stop LoRA training runs."""

    def __init__(self) -> None:
        self.status_info = self._compute_status()

    @staticmethod
    def _compute_status() -> dict[str, Any]:
        configured = settings.ai_toolkit_configured
        return {
            "configured": configured,
            "toolkit_path": settings.ai_toolkit_path or None,
            "python": settings.ai_toolkit_python or None,
            "supported_archs": sorted(ARCH_DEFAULTS.keys()),
            "error": None if configured else (
                "Set AI_TOOLKIT_PATH and AI_TOOLKIT_PYTHON to enable training"
            ),
        }

    def status(self) -> dict[str, Any]:
        return dict(self.status_info)

    @staticmethod
    def presets() -> list[dict[str, Any]]:
        return [
            {"id": name, **values}
            for name, values in TRAINING_PRESETS.items()
        ]

    @staticmethod
    def hardware_presets() -> dict[str, Any]:
        return {
            "unknown": {"recommended": "conservative"},
            "small": {"max_vram_gb": 8, "recommended": "conservative",
                      "notes": "512px resolution, rank <= 16"},
            "medium": {"max_vram_gb": 16, "recommended": "balanced",
                       "notes": "768px resolution, rank <= 32"},
            "large": {"min_vram_gb": 24, "recommended": "quality",
                      "notes": "1024px resolution, higher ranks OK"},
        }

    def preflight(self, options: dict[str, Any]) -> dict[str, Any]:
        report = advise(options)
        if not settings.ai_toolkit_configured:
            report.checks.insert(0, advise.__self__ and _toolkit_check())
            report.verdict = "blocked" if report.verdict == "ok" else report.verdict
        return report.to_dict()

    def find_trained_lora(self, output_dir: Path) -> Path | None:
        from app.core.security.paths import is_safetensors

        candidates = [
            p for p in output_dir.glob("*.safetensors")
            if p.is_file() and is_safetensors(p)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)


def _toolkit_check():
    from app.training.preflight import PreflightCheck

    return PreflightCheck(
        name="AI Toolkit",
        passed=False,
        detail="AI Toolkit is not configured (AI_TOOLKIT_PATH / AI_TOOLKIT_PYTHON)",
        severity="error",
    )


__all__ = [
    "ARCH_DEFAULTS",
    "AIToolkitProcess",
    "DEFAULT_ARCH",
    "LoRATrainingService",
    "TRAINING_PRESETS",
    "TrainingError",
    "build_training_config",
]
