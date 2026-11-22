"""Tests for app/training/service.py: config building, subprocess management and service status."""

from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path
from typing import Any, cast

from app.core.config import settings
from app.training import preflight as preflight_module, service as service_module
from app.training.hardware import GPUInfo, HardwareInfo
from app.training.service import (
    TRAINING_PRESETS,
    AIToolkitProcess,
    LoRATrainingService,
    TrainingError,
    build_training_config,
)

# --- fakes ---------------------------------------------------------------------


class FakeProcess:
    """Stands in for subprocess.Popen; records the call and feeds scripted stdout lines."""

    def __init__(self, argv: list[str], cwd: str | None = None, **kwargs: Any) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.kwargs = kwargs
        self.pid = 4242
        self.stdout: Any = iter(())
        self._exit_code: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[float | None] = []
        self.wait_raises_timeout = False

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        self._exit_code = 0

    def wait(self, timeout: float | None = None) -> int | None:
        self.wait_timeouts.append(timeout)
        if self.wait_raises_timeout:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=0.0 if timeout is None else timeout)
        return self._exit_code

    def kill(self) -> None:
        self.killed = True


def attach(proc: AIToolkitProcess, fake: FakeProcess) -> None:
    """Attach a scripted process to an AIToolkitProcess without touching real subprocesses."""
    proc.process = cast("subprocess.Popen[str]", fake)


def make_subprocess_stub(stdout_lines: list[str]) -> tuple[types.SimpleNamespace, list[FakeProcess]]:
    """Replace the service module's subprocess with a stub that hands out scripted processes."""
    created: list[FakeProcess] = []

    def popen(argv: list[str], cwd: str | None = None, **kwargs: Any) -> FakeProcess:
        proc = FakeProcess(argv, cwd=cwd, **kwargs)
        proc.stdout = iter(stdout_lines)
        created.append(proc)
        return proc

    stub = types.SimpleNamespace(
        Popen=popen,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
        TimeoutExpired=subprocess.TimeoutExpired,
        CREATE_NEW_PROCESS_GROUP=0x00000200,
        run=None,  # set per test for stop()
    )
    return stub, created


def make_process(base: Path) -> AIToolkitProcess:
    return AIToolkitProcess(
        python_exe=str(base / "python.exe"),
        toolkit_path=base / "toolkit",
        config_path=base / "config.json",
        log_path=base / "log.txt",
    )


def write_safetensors(path: Path, mtime: float) -> None:
    """Write a minimal valid safetensors file (8-byte LE header length + JSON dict)."""
    header = json.dumps({"__metadata__": {}}).encode("utf-8")
    path.write_bytes(len(header).to_bytes(8, "little") + header)
    os.utime(path, (mtime, mtime))


def ok_hardware() -> HardwareInfo:
    return HardwareInfo(
        gpus=[GPUInfo(vendor="nvidia", name="RTX 4090", vram_total_mb=24576, source="nvidia-smi")],
        ram_total_mb=32768,
    )


def risky_hardware() -> HardwareInfo:
    return HardwareInfo(
        gpus=[GPUInfo(vendor="nvidia", name="RTX 3060", vram_total_mb=8192, source="nvidia-smi")],
        ram_total_mb=32768,
    )


def unconfigured_toolkit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ai_toolkit_path", "")
    monkeypatch.setattr(settings, "ai_toolkit_python", "")


def configured_toolkit(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    toolkit = tmp_path / "toolkit"
    toolkit.mkdir()
    (toolkit / "run.py").write_text("# toolkit entry point\n", encoding="utf-8")
    python_exe = tmp_path / "python.exe"
    python_exe.write_text("x", encoding="utf-8")
    monkeypatch.setattr(settings, "ai_toolkit_path", str(toolkit))
    monkeypatch.setattr(settings, "ai_toolkit_python", str(python_exe))
    return toolkit, python_exe


def patch_hardware(monkeypatch, hardware: HardwareInfo) -> None:
    monkeypatch.setattr(preflight_module, "detect_hardware", lambda: hardware)
    monkeypatch.setattr(preflight_module, "detect_disk_free_gb", lambda: 100.0)


# --- build_training_config --------------------------------------------------------


def test_build_config_sdxl_defaults(tmp_path):
    out = tmp_path / "runs" / "model.safetensors"
    config = build_training_config(
        project_name="hero", dataset_path="/data/hero", output_path=str(out)
    )
    top = config["config"]
    assert top["name"] == "hero"
    proc = top["process"][0]
    assert proc["type"] == "sd_trainer"
    assert proc["training_folder"] == str(tmp_path / "runs")
    assert proc["device"] == "cuda:0"
    assert proc["trigger_word"] == ""
    assert proc["network"] == {"type": "lora", "linear": 16, "linear_alpha": 16}
    assert proc["save"] == {"dtype": "float16", "save_every": 250, "max_step_saves_to_keep": 4}
    dataset = proc["datasets"][0]
    assert dataset["folder_path"] == "/data/hero"
    assert dataset["caption_ext"] == "txt"
    assert dataset["resolution"] == [1024, 1024]
    train = proc["train"]
    assert train["batch_size"] == 1
    assert train["steps"] == 2000
    assert train["lr"] == 1e-4
    assert train["noise_scheduler"] == "ddpm"
    assert train["dtype"] == "bf16"
    assert proc["model"] == {
        "name_or_path": "stabilityai/stable-diffusion-xl-base-1.0",
        "is_flux": False,
        "quantize": False,
    }
    assert proc["disable_sampling"] is True


def test_build_config_flux(tmp_path):
    config = build_training_config(
        project_name="f", dataset_path="/d", output_path=str(tmp_path / "m.safetensors"), arch="flux"
    )
    proc = config["config"]["process"][0]
    assert proc["model"] == {"name_or_path": "black-forest-labs/FLUX.1-dev", "is_flux": True, "quantize": True}


def test_build_config_unknown_arch_falls_back_to_sdxl(tmp_path):
    config = build_training_config(
        project_name="f", dataset_path="/d", output_path=str(tmp_path / "m.safetensors"), arch="bogus"
    )
    proc = config["config"]["process"][0]
    assert proc["model"]["name_or_path"] == "stabilityai/stable-diffusion-xl-base-1.0"
    assert proc["train"]["noise_scheduler"] == "ddpm"


def test_build_config_overrides(tmp_path):
    out = tmp_path / "out" / "m.safetensors"
    config = build_training_config(
        project_name="p",
        dataset_path="/d",
        output_path=str(out),
        base_model="custom/model",
        resolution=[512, 512],
        lora_rank=8,
        lora_alpha=4,
        steps=100,
        learning_rate=3e-4,
        batch_size=2,
        save_every=10,
        max_saves_to_keep=1,
        device="cuda:1",
    )
    proc = config["config"]["process"][0]
    assert proc["model"]["name_or_path"] == "custom/model"
    assert proc["datasets"][0]["resolution"] == [512, 512]
    assert proc["network"] == {"type": "lora", "linear": 8, "linear_alpha": 4}
    train = proc["train"]
    assert train["steps"] == 100
    assert train["lr"] == 3e-4
    assert train["batch_size"] == 2
    assert proc["save"] == {"dtype": "float16", "save_every": 10, "max_step_saves_to_keep": 1}
    assert proc["device"] == "cuda:1"


def test_build_config_sampling_block(tmp_path):
    config = build_training_config(
        project_name="p",
        dataset_path="/d",
        output_path=str(tmp_path / "m.safetensors"),
        sample_every=50,
        sample_prompts=["a cat", "a dog"],
    )
    proc = config["config"]["process"][0]
    assert proc["sample"] == {
        "sampling_every": 50,
        "guidance_scale": 4.0,
        "sample_steps": 20,
        "prompts": ["a cat", "a dog"],
    }
    assert "disable_sampling" not in proc


def test_build_config_sample_every_without_prompts_disables_sampling(tmp_path):
    config = build_training_config(
        project_name="p", dataset_path="/d", output_path=str(tmp_path / "m.safetensors"), sample_every=50
    )
    assert config["config"]["process"][0]["disable_sampling"] is True


# --- TrainingError ------------------------------------------------------------------


def test_training_error_default_and_custom_status():
    default = TrainingError("boom")
    assert str(default) == "boom"
    assert default.status_code == 400
    custom = TrainingError("nope", status_code=422)
    assert custom.status_code == 422



# --- AIToolkitProcess: start and stream ----------------------------------------------


def test_start_uses_fixed_argv_and_cwd(tmp_path, monkeypatch):
    lines = ["step 12/40\n", "loss=0.75\n"]
    stub, created = make_subprocess_stub(lines)
    monkeypatch.setattr(service_module, "subprocess", stub)
    proc = make_process(tmp_path)
    proc.start()
    assert proc._thread is not None
    proc._thread.join(timeout=5)
    assert not proc._thread.is_alive()

    fake = created[0]
    assert fake.argv == [str(tmp_path / "python.exe"), "run.py", str(tmp_path / "config.json")]
    assert fake.cwd == str(tmp_path / "toolkit")
    assert fake.kwargs["stdout"] is subprocess.PIPE
    assert fake.kwargs["stderr"] is subprocess.STDOUT
    assert fake.kwargs["text"] is True
    assert fake.kwargs["encoding"] == "utf-8"
    assert fake.kwargs["errors"] == "replace"
    assert fake.kwargs["creationflags"] == 0x00000200

    # The streamed lines were parsed and written to the log file.
    assert proc.snapshot() == {"current_step": 12, "total_steps": 40, "loss": 0.75, "last_line": "loss=0.75"}
    assert (tmp_path / "log.txt").read_text(encoding="utf-8") == "step 12/40\nloss=0.75\n"


def test_parse_step_loss_and_truncation(tmp_path):
    proc = make_process(tmp_path)
    proc._parse("step 7/99\n")
    assert (proc.current_step, proc.total_steps) == (7, 99)
    proc._parse("loss: 0.5")
    assert proc.loss == 0.5
    proc._parse("x" * 300 + "\n")
    assert len(proc.last_line) == 200


def test_parse_invalid_loss_keeps_previous(tmp_path):
    proc = make_process(tmp_path)
    proc._parse("loss=0.5\n")
    proc._parse("loss=..\n")  # matches the regex but float("..") raises; must be ignored
    assert proc.loss == 0.5


def test_snapshot_initial_values(tmp_path):
    proc = make_process(tmp_path)
    assert proc.snapshot() == {"current_step": 0, "total_steps": 0, "loss": None, "last_line": ""}


# --- AIToolkitProcess: is_running and stop ---------------------------------------------


def test_is_running(tmp_path):
    proc = make_process(tmp_path)
    assert proc.is_running() is False

    running = FakeProcess(["x"])
    attach(proc, running)
    assert proc.is_running() is True

    running._exit_code = 0
    assert proc.is_running() is False


def test_stop_without_process_returns_true(tmp_path):
    assert make_process(tmp_path).stop() is True


def test_stop_already_exited_returns_true(tmp_path, monkeypatch):
    stub, _ = make_subprocess_stub([])
    runs: list[list[str]] = []

    def run(argv, **kwargs):
        runs.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    stub.run = run
    monkeypatch.setattr(service_module, "subprocess", stub)

    proc = make_process(tmp_path)
    fake = FakeProcess(["x"])
    fake._exit_code = 0
    attach(proc, fake)
    assert proc.stop() is True
    assert runs == []


def test_stop_windows_uses_taskkill(tmp_path, monkeypatch):
    stub, _ = make_subprocess_stub([])
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    stub.run = run
    monkeypatch.setattr(service_module, "subprocess", stub)
    monkeypatch.setattr(os, "name", "nt")

    proc = make_process(tmp_path)
    attach(proc, FakeProcess(["x"]))
    assert proc.stop(timeout=5.0) is True
    argv, kwargs = calls[0]
    assert argv == ["taskkill", "/F", "/T", "/PID", "4242"]
    assert kwargs == {"capture_output": True, "check": False, "timeout": 5.0}


def test_stop_posix_terminates_and_waits(tmp_path, monkeypatch):
    stub, _ = make_subprocess_stub([])
    monkeypatch.setattr(service_module, "subprocess", stub)
    monkeypatch.setattr(os, "name", "posix")

    proc = make_process(tmp_path)
    fake = FakeProcess(["x"])
    attach(proc, fake)
    assert proc.stop(timeout=7.5) is True
    assert fake.terminated is True
    assert fake.wait_timeouts == [7.5]


def test_stop_posix_timeout_kills(tmp_path, monkeypatch):
    stub, _ = make_subprocess_stub([])
    monkeypatch.setattr(service_module, "subprocess", stub)
    monkeypatch.setattr(os, "name", "posix")

    proc = make_process(tmp_path)
    fake = FakeProcess(["x"])
    fake.wait_raises_timeout = True
    attach(proc, fake)
    assert proc.stop(timeout=2.0) is True
    assert fake.killed is True


def test_stop_oserror_returns_false(tmp_path, monkeypatch):
    stub, _ = make_subprocess_stub([])

    def run(argv, **kwargs):
        raise OSError("taskkill missing")

    stub.run = run
    monkeypatch.setattr(service_module, "subprocess", stub)
    monkeypatch.setattr(os, "name", "nt")

    proc = make_process(tmp_path)
    attach(proc, FakeProcess(["x"]))
    assert proc.stop() is False


# --- registry shutdown -----------------------------------------------------------------


def test_stop_all_trainers_stops_registered(monkeypatch):
    registry = {make_process(Path("a")), make_process(Path("b"))}
    monkeypatch.setattr(service_module, "_REGISTRY", registry)
    service_module._stop_all_trainers()  # no child process yet, so stop() is a no-op returning True
    assert len(registry) == 2



# --- LoRATrainingService status ------------------------------------------------------------


def test_status_unconfigured(monkeypatch):
    unconfigured_toolkit(monkeypatch)
    status = LoRATrainingService().status()
    assert status["configured"] is False
    assert status["toolkit_path"] is None
    assert status["python"] is None
    assert status["error"] == "Set AI_TOOLKIT_PATH and AI_TOOLKIT_PYTHON to enable training"
    assert status["supported_archs"] == ["flux", "sdxl"]


def test_status_configured(monkeypatch, tmp_path):
    toolkit, python_exe = configured_toolkit(monkeypatch, tmp_path)
    status = LoRATrainingService().status()
    assert status["configured"] is True
    assert status["toolkit_path"] == str(toolkit)
    assert status["python"] == str(python_exe)
    assert status["error"] is None


def test_status_returns_copy(monkeypatch):
    unconfigured_toolkit(monkeypatch)
    service = LoRATrainingService()
    first = service.status()
    first["error"] = "tampered"
    assert service.status()["error"] != "tampered"


# --- presets -------------------------------------------------------------------------------


def test_presets_match_training_presets():
    presets = LoRATrainingService.presets()
    assert [preset["id"] for preset in presets] == list(TRAINING_PRESETS)
    character = next(p for p in presets if p["id"] == "character")
    assert character["steps"] == 2000
    assert character["lora_rank"] == 32


def test_hardware_presets():
    assert LoRATrainingService.hardware_presets() == {
        "unknown": {"recommended": "conservative"},
        "small": {
            "max_vram_gb": 8,
            "recommended": "conservative",
            "notes": "512px resolution, rank <= 16",
        },
        "medium": {
            "max_vram_gb": 16,
            "recommended": "balanced",
            "notes": "768px resolution, rank <= 32",
        },
        "large": {
            "min_vram_gb": 24,
            "recommended": "quality",
            "notes": "1024px resolution, higher ranks OK",
        },
    }


# --- preflight wiring -------------------------------------------------------------------------


def test_preflight_unconfigured_blocks_ok_report(monkeypatch):
    unconfigured_toolkit(monkeypatch)
    patch_hardware(monkeypatch, ok_hardware())
    report = LoRATrainingService().preflight({})
    assert report["verdict"] == "blocked"
    assert report["can_proceed"] is False
    first = report["checks"][0]
    assert first["name"] == "AI Toolkit"
    assert first["passed"] is False
    assert first["severity"] == "error"


def test_preflight_unconfigured_keeps_risky_verdict(monkeypatch):
    unconfigured_toolkit(monkeypatch)
    patch_hardware(monkeypatch, risky_hardware())
    report = LoRATrainingService().preflight({})
    assert report["verdict"] == "risky"
    assert report["checks"][0]["name"] == "AI Toolkit"


def test_preflight_configured_adds_no_toolkit_check(monkeypatch, tmp_path):
    configured_toolkit(monkeypatch, tmp_path)
    patch_hardware(monkeypatch, ok_hardware())
    report = LoRATrainingService().preflight({})
    assert report["verdict"] == "ok"
    names = [check["name"] for check in report["checks"]]
    assert "AI Toolkit" not in names


# --- find_trained_lora --------------------------------------------------------------------------


def test_find_trained_lora_empty_dir(tmp_path):
    assert LoRATrainingService().find_trained_lora(tmp_path) is None


def test_find_trained_lora_picks_newest_valid(tmp_path):
    older = tmp_path / "old.safetensors"
    newer = tmp_path / "new.safetensors"
    write_safetensors(older, 1000.0)
    write_safetensors(newer, 2000.0)
    assert LoRATrainingService().find_trained_lora(tmp_path) == newer


def test_find_trained_lora_ignores_invalid_header(tmp_path):
    valid = tmp_path / "valid.safetensors"
    invalid = tmp_path / "invalid.safetensors"
    write_safetensors(valid, 1000.0)
    invalid.write_bytes(b"this is not a safetensors file")
    os.utime(invalid, (3000.0, 3000.0))  # newer than the valid one, but must be ignored
    assert LoRATrainingService().find_trained_lora(tmp_path) == valid


def test_find_trained_lora_only_invalid_returns_none(tmp_path):
    invalid = tmp_path / "bad.safetensors"
    invalid.write_bytes(b"\x00\x01")  # shorter than the 8-byte header length field
    assert LoRATrainingService().find_trained_lora(tmp_path) is None

