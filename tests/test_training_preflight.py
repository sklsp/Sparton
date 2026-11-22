"""Tests for app/training/preflight.py, the pre-launch config advisor."""

from __future__ import annotations

import pytest

from app.training import hardware as hw_module, preflight


def make_hardware(vram_mb: int | None = 24576, ram_mb: int = 32768) -> hw_module.HardwareInfo:
    return hw_module.HardwareInfo(
        gpus=[hw_module.GPUInfo(
            vendor="nvidia", name="RTX 4090", vram_total_mb=vram_mb, source="nvidia-smi",
        )],
        cpu_name="Test CPU",
        cpu_cores=16,
        ram_total_mb=ram_mb,
        ram_available_mb=8192,
        disk_free_gb=500.0,
        cuda_available=True,
    )


def check_names(report):
    return [check.name for check in report.checks]


def find_check(report, name):
    for check in report.checks:
        if check.name == name:
            return check
    raise AssertionError(f"no check named {name!r} in {check_names(report)}")


# --- dataclasses ---------------------------------------------------------------


def test_preflight_check_defaults_and_to_dict():
    check = preflight.PreflightCheck(name="Disk space", passed=True)
    assert check.detail == ""
    assert check.severity == "info"
    assert check.to_dict() == {
        "name": "Disk space",
        "passed": True,
        "detail": "",
        "severity": "info",
    }


def test_preflight_report_defaults():
    report = preflight.PreflightReport()
    assert report.checks == []
    assert report.verdict == "ok"
    assert report.summary == ""
    assert report.recommendations == []
    assert report.estimated_vram_mb is None
    assert report.hardware is None


def test_can_proceed_is_only_false_for_blocked_and_unsupported():
    for verdict in ("ok", "heavy", "risky"):
        assert preflight.PreflightReport(verdict=verdict).can_proceed is True
    for verdict in ("blocked", "unsupported"):
        assert preflight.PreflightReport(verdict=verdict).can_proceed is False


def test_preflight_report_to_dict_includes_checks_and_can_proceed():
    check = preflight.PreflightCheck(name="VRAM", passed=False, detail="tight", severity="warning")
    report = preflight.PreflightReport(
        checks=[check], verdict="risky", summary="s", recommendations=["r"],
        estimated_vram_mb=20000, hardware={"gpus": []},
    )
    assert report.to_dict() == {
        "checks": [{"name": "VRAM", "passed": False, "detail": "tight", "severity": "warning"}],
        "verdict": "risky",
        "summary": "s",
        "recommendations": ["r"],
        "estimated_vram_mb": 20000,
        "hardware": {"gpus": []},
        "can_proceed": True,
    }


# --- estimate_vram_mb ----------------------------------------------------------


def test_estimate_defaults_to_high_resolution_single_batch_rank_16():
    assert preflight.estimate_vram_mb({}) == 14336


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [
        ([512, 512], 6144),    # low bucket
        ([768, 768], 9216),    # medium bucket
        ([1024, 512], 14336),  # high bucket: the largest side decides
        ([1536, 1536], 22528),  # extreme bucket
    ],
)
def test_estimate_resolution_buckets(resolution, expected):
    assert preflight.estimate_vram_mb({"resolution": resolution}) == expected


def test_estimate_batch_size_adds_1200_mb_per_extra_slot():
    base = preflight.estimate_vram_mb({"resolution": [768, 768]})
    with_batch = preflight.estimate_vram_mb({"resolution": [768, 768], "batch_size": 3})
    assert with_batch - base == 2 * 1200


def test_estimate_lora_rank_above_16_adds_memory():
    # rank 32: (32-16)/16*150 = 150 MB over the rank-16 baseline
    assert preflight.estimate_vram_mb({"lora_rank": 32}) == 14336 + 150


def test_estimate_lora_rank_below_16_is_clamped():
    assert preflight.estimate_vram_mb({"lora_rank": 8}) == 14336

# --- advise: no GPU ------------------------------------------------------------


def test_advise_without_gpu_is_unsupported():
    report = preflight.advise({}, hardware=hw_module.HardwareInfo())
    assert report.verdict == "unsupported"
    assert report.can_proceed is False
    # early return: nothing after the Accelerator check is appended
    assert check_names(report) == ["Accelerator"]

    accelerator = find_check(report, "Accelerator")
    assert accelerator.passed is False
    assert accelerator.severity == "error"
    assert accelerator.detail == (
        "No GPU detected. Training on CPU is impractically slow for SDXL/Flux-class models."
    )
    assert report.summary == "No usable GPU detected, so training is not practical on this machine."
    assert report.recommendations == [
        "Install/enable GPU drivers, or run training on a different machine."
    ]
    assert report.estimated_vram_mb is None


def test_advise_detects_hardware_when_not_passed(monkeypatch):
    hw = make_hardware()
    monkeypatch.setattr(preflight, "detect_hardware", lambda: hw)
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({})
    assert report.hardware == hw.to_dict()


# --- advise: VRAM branches -----------------------------------------------------


def test_advise_enough_vram_is_ok(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({}, hardware=make_hardware(vram_mb=24576))
    assert report.verdict == "ok"
    assert report.can_proceed is True
    assert report.summary == "Configuration looks safe for this machine."
    assert report.recommendations == []
    assert report.estimated_vram_mb == 14336

    accelerator = find_check(report, "Accelerator")
    assert accelerator.passed is True
    assert accelerator.detail == "RTX 4090 (nvidia) via nvidia-smi"

    vram = find_check(report, "VRAM")
    assert vram.passed is True
    assert vram.severity == "info"
    assert vram.detail == "~14 GB estimated vs 24 GB available"

    ram = find_check(report, "System RAM")
    assert ram.passed is True and ram.detail == "32 GB total"

    disk = find_check(report, "Disk space")
    assert disk.passed is True and disk.detail == "500.0 GB free"

    lr = find_check(report, "Learning rate")
    assert lr.passed is True and lr.severity == "info" and lr.detail == "0.0001"

    steps = find_check(report, "Steps")
    assert steps.passed is True and steps.detail == "2000"


def test_advise_tight_vram_is_heavy_with_recommendation(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({}, hardware=make_hardware(vram_mb=16384))
    assert report.verdict == "heavy"
    assert report.can_proceed is True
    assert report.summary == "Training may be tight on resources but should fit."

    vram = find_check(report, "VRAM")
    assert vram.passed is True
    assert vram.severity == "warning"
    assert vram.detail == "~14 GB estimated vs 16 GB available (tight)"
    assert report.recommendations == [
        "Reduce batch size to 1, lower resolution, or enable gradient "
        "accumulation to trade speed for memory."
    ]


def test_advise_vram_exceeded_is_risky(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({}, hardware=make_hardware(vram_mb=8192))
    assert report.verdict == "risky"
    assert report.can_proceed is True
    assert report.summary == "High risk of out-of-memory. Reduce load before starting."

    vram = find_check(report, "VRAM")
    assert vram.passed is False
    assert vram.severity == "warning"
    assert vram.detail == "~14 GB estimated vs 8 GB available"
    assert report.recommendations == [
        "Estimated usage exceeds the GPU's 8 GB. Cut resolution first (biggest lever), then batch size."
    ]


def test_advise_unknown_vram_warns_but_stays_ok(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({}, hardware=make_hardware(vram_mb=None))
    assert report.verdict == "ok"

    vram = find_check(report, "VRAM")
    assert vram.passed is True
    assert vram.severity == "warning"
    assert vram.detail == "GPU found but VRAM size unknown; ~14 GB will be needed"

# --- advise: RAM, disk, learning rate, steps -----------------------------------


def test_advise_low_ram_downgrades_ok_to_heavy(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({}, hardware=make_hardware(vram_mb=24576, ram_mb=8192))
    assert report.verdict == "heavy"

    ram = find_check(report, "System RAM")
    assert ram.passed is True
    assert ram.severity == "warning"
    assert ram.detail == "8 GB total, below the 16 GB comfort zone for SDXL"


def test_advise_low_disk_blocks_and_stops_early(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 5.0)
    report = preflight.advise({}, hardware=make_hardware())
    assert report.verdict == "blocked"
    assert report.can_proceed is False
    assert report.summary == "Not enough disk space for training checkpoints."
    # early return: no Learning rate or Steps checks are appended
    assert check_names(report) == ["Accelerator", "VRAM", "System RAM", "Disk space"]

    disk = find_check(report, "Disk space")
    assert disk.passed is False
    assert disk.severity == "error"
    assert disk.detail == "5.0 GB free, checkpoints need 5-15 GB"


def test_advise_missing_disk_reading_skips_the_disk_check(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: None)
    report = preflight.advise({}, hardware=make_hardware())
    assert "Disk space" not in check_names(report)
    assert report.verdict == "ok"


def test_advise_aggressive_learning_rate_warns_and_heavies(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({"learning_rate": 1e-3}, hardware=make_hardware())
    assert report.verdict == "heavy"

    lr = find_check(report, "Learning rate")
    assert lr.passed is True
    assert lr.severity == "warning"
    assert lr.detail == "0.001 is aggressive for LoRA; expect instability"


def test_advise_very_low_learning_rate_warns_but_stays_ok(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({"learning_rate": 5e-6}, hardware=make_hardware())
    assert report.verdict == "ok"

    lr = find_check(report, "Learning rate")
    assert lr.severity == "warning"
    assert lr.detail == "5e-06 is very low; training may barely move"


def test_advise_long_step_count_warns_but_stays_ok(monkeypatch):
    monkeypatch.setattr(preflight, "detect_disk_free_gb", lambda: 500.0)
    report = preflight.advise({"steps": 20000}, hardware=make_hardware())
    assert report.verdict == "ok"

    steps = find_check(report, "Steps")
    assert steps.passed is True
    assert steps.severity == "warning"
    assert steps.detail == "20000 steps is a long run; consider checkpoints + early review"


# --- diagnose_failure ----------------------------------------------------------


def test_diagnose_cuda_oom():
    result = preflight.diagnose_failure("CUDA out of memory. Tried to allocate 2.00 GiB")
    assert result["explanation"] == "Training ran out of GPU memory."
    assert result["recommendation"] == (
        "Reduce batch size to 1, lower the resolution, or shorten the max "
        "sequence length. The last successful checkpoint is still valid."
    )


def test_diagnose_plain_out_of_memory_matches_the_same_branch():
    result = preflight.diagnose_failure("RuntimeError: out of memory")
    assert result["explanation"] == "Training ran out of GPU memory."


def test_diagnose_disk_full():
    result = preflight.diagnose_failure("OSError: [Errno 28] No space left on device")
    assert result["explanation"] == "The disk holding the training output ran out of space."
    assert result["recommendation"] == "Free up space or change the output directory, then resume."


def test_diagnose_missing_module():
    result = preflight.diagnose_failure("ModuleNotFoundError: No module named 'diffusers'")
    assert result["explanation"] == "A dependency is missing from the AI Toolkit environment."
    assert result["recommendation"] == (
        "Install the missing package inside AI_TOOLKIT_PYTHON's environment."
    )


def test_diagnose_missing_file():
    result = preflight.diagnose_failure("FileNotFoundError: [Errno 2] No such file or directory")
    assert result["explanation"] == "A file the training config references was not found."
    assert result["recommendation"] == "Check the dataset folder and base model path exist."


def test_diagnose_download_failure():
    result = preflight.diagnose_failure("ConnectionError: max retries exceeded with url: /models")
    assert result["explanation"] == "A download (usually the base model) failed mid-run."
    assert result["recommendation"] == "Check your internet connection, or pre-download the model."


def test_diagnose_system_ram_oom():
    # no spaces in "outofmemoryerror", so this skips the GPU OOM pattern on purpose
    result = preflight.diagnose_failure("OutOfMemoryError: cannot allocate 4GB")
    assert result["explanation"] == "The system ran out of RAM."
    assert result["recommendation"] == (
        "Close other applications, or reduce dataloader workers/batch size."
    )


def test_diagnose_unknown_error_falls_back_to_generic_advice():
    result = preflight.diagnose_failure("segfault at 0xdeadbeef")
    assert result["explanation"] == "Training failed. The log tail has the details."
    assert result["recommendation"] == (
        "Check training.log; common causes are OOM, disk space, or a bad model path."
    )


def test_diagnose_truncates_raw_error_to_2000_chars():
    raw = "x" * 3000
    result = preflight.diagnose_failure(raw)
    assert result["raw"] == raw[:2000]
    assert len(result["raw"]) == 2000


