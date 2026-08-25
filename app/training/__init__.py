"""Leonidas — LoRA training domain."""

from app.training.hardware import HardwareInfo, detect_hardware
from app.training.preflight import PreflightReport, advise, diagnose_failure, estimate_vram_mb
from app.training.service import (
    ARCH_DEFAULTS,
    AIToolkitProcess,
    TRAINING_PRESETS,
    TrainingError,
    build_training_config,
)

__all__ = [
    "ARCH_DEFAULTS",
    "AIToolkitProcess",
    "HardwareInfo",
    "PreflightReport",
    "TRAINING_PRESETS",
    "TrainingError",
    "advise",
    "build_training_config",
    "detect_hardware",
    "diagnose_failure",
    "estimate_vram_mb",
]
