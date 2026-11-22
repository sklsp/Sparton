"""Tests for app/training/hardware.py, the best-effort machine snapshot used by preflight."""

from __future__ import annotations

import io
import os
import platform
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.training import hardware

# --- dataclasses ---------------------------------------------------------------


def test_gpu_info_to_dict():
    gpu = hardware.GPUInfo(
        vendor="nvidia", name="NVIDIA GeForce RTX 4090",
        vram_total_mb=24564, vram_free_mb=18321, source="nvidia-smi",
    )
    assert gpu.to_dict() == {
        "vendor": "nvidia",
        "name": "NVIDIA GeForce RTX 4090",
        "vram_total_mb": 24564,
        "vram_free_mb": 18321,
        "source": "nvidia-smi",
    }


def test_gpu_info_defaults():
    gpu = hardware.GPUInfo(vendor="unknown", name="GPU")
    assert gpu.vram_total_mb is None
    assert gpu.vram_free_mb is None
    assert gpu.source == ""


@pytest.mark.parametrize(
    ("gpus", "rocm_available", "expected"),
    [
        ([], False, "cpu"),
        ([hardware.GPUInfo(vendor="nvidia", name="RTX 4090")], False, "cuda"),
        ([hardware.GPUInfo(vendor="amd", name="RX 7900 XTX")], True, "rocm"),
        ([hardware.GPUInfo(vendor="amd", name="RX 7900 XTX")], False, "amd"),
        ([hardware.GPUInfo(vendor="unknown", name="Arc A770")], False, "unknown"),
    ],
)
def test_hardware_info_accelerator(gpus, rocm_available, expected):
    info = hardware.HardwareInfo(gpus=gpus, rocm_available=rocm_available)
    assert info.accelerator == expected


def test_hardware_info_primary_gpu_and_vram():
    empty = hardware.HardwareInfo()
    assert empty.primary_gpu is None
    assert empty.vram_total_mb is None

    gpu = hardware.GPUInfo(vendor="nvidia", name="RTX 4090", vram_total_mb=24564)
    info = hardware.HardwareInfo(gpus=[gpu])
    assert info.primary_gpu is gpu
    assert info.vram_total_mb == 24564


def test_hardware_info_to_dict():
    gpu = hardware.GPUInfo(vendor="nvidia", name="RTX 4090", vram_total_mb=24564, source="torch")
    info = hardware.HardwareInfo(
        gpus=[gpu], cpu_name="CPU X", cpu_cores=8, ram_total_mb=16384,
        ram_available_mb=8192, disk_free_gb=500.0, cuda_available=True,
    )
    assert info.to_dict() == {
        "gpus": [gpu.to_dict()],
        "primary_gpu": gpu.to_dict(),
        "accelerator": "cuda",
        "cpu_name": "CPU X",
        "cpu_cores": 8,
        "ram_total_mb": 16384,
        "ram_available_mb": 8192,
        "disk_free_gb": 500.0,
        "cuda_available": True,
        "rocm_available": False,
    }


def test_hardware_info_to_dict_without_gpus():
    info = hardware.HardwareInfo()
    assert info.to_dict()["gpus"] == []
    assert info.to_dict()["primary_gpu"] is None



# --- ComfyUI probe -------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def json(self):
        if self._error is not None:
            raise self._error
        return self._payload


def test_detect_via_comfyui_not_running(monkeypatch):
    def refused(url, timeout=None):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("requests.get", refused)
    assert hardware._detect_via_comfyui("http://127.0.0.1:8188") == []


def test_detect_via_comfyui_bad_json(monkeypatch):
    monkeypatch.setattr(
        "requests.get", lambda url, timeout=None: _FakeResponse(error=ValueError("no json"))
    )
    assert hardware._detect_via_comfyui("http://127.0.0.1:8188") == []


def test_detect_via_comfyui_missing_devices_key(monkeypatch):
    monkeypatch.setattr(
        "requests.get", lambda url, timeout=None: _FakeResponse(payload={"ok": True})
    )
    assert hardware._detect_via_comfyui("http://127.0.0.1:8188") == []


def test_detect_via_comfyui_parses_devices(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        seen["timeout"] = timeout
        return _FakeResponse(payload={"devices": [
            {"name": "NVIDIA GeForce RTX 4090", "vram_total": 2 * 1024 ** 3, "vram_free": 1024 ** 3},
            {"name": "AMD Radeon RX 7900 XTX", "vram_total": 24 * 1024 ** 3, "vram_free": None},
            {},
        ]})

    monkeypatch.setattr("requests.get", fake_get)
    gpus = hardware._detect_via_comfyui("http://127.0.0.1:8188/")
    assert seen["url"] == "http://127.0.0.1:8188/system_stats"  # trailing slash stripped
    assert seen["timeout"] == 3.0
    assert [gpu.to_dict() for gpu in gpus] == [
        {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090",
         "vram_total_mb": 2048, "vram_free_mb": 1024, "source": "comfyui"},
        {"vendor": "amd", "name": "AMD Radeon RX 7900 XTX",
         "vram_total_mb": 24576, "vram_free_mb": None, "source": "comfyui"},
        {"vendor": "unknown", "name": "GPU",
         "vram_total_mb": None, "vram_free_mb": None, "source": "comfyui"},
    ]



# --- nvidia-smi probe ----------------------------------------------------------


def _smi(stdout):
    return SimpleNamespace(stdout=stdout, returncode=0)


def test_nvidia_smi_binary_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert hardware._detect_via_nvidia_smi() == []


def test_nvidia_smi_one_gpu(monkeypatch):
    calls = {}

    def fake_run(argv, **kwargs):
        calls["argv"] = argv
        calls.update(kwargs)
        return _smi("NVIDIA GeForce RTX 4090, 24564, 18321\n")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(subprocess, "run", fake_run)
    gpus = hardware._detect_via_nvidia_smi()
    assert calls["argv"] == [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.free",
        "--format=csv,noheader,nounits",
    ]
    assert calls["timeout"] == 5
    assert calls["check"] is False
    assert [gpu.to_dict() for gpu in gpus] == [
        {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090",
         "vram_total_mb": 24564, "vram_free_mb": 18321, "source": "nvidia-smi"},
    ]


def test_nvidia_smi_two_gpus(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    stdout = (
        "NVIDIA GeForce RTX 4090, 24564, 18321\n"
        "NVIDIA A100-SXM4-80GB, 81920, 70000\n"
    )
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _smi(stdout))
    gpus = hardware._detect_via_nvidia_smi()
    assert [gpu.to_dict() for gpu in gpus] == [
        {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090",
         "vram_total_mb": 24564, "vram_free_mb": 18321, "source": "nvidia-smi"},
        {"vendor": "nvidia", "name": "NVIDIA A100-SXM4-80GB",
         "vram_total_mb": 81920, "vram_free_mb": 70000, "source": "nvidia-smi"},
    ]


def test_nvidia_smi_odd_spacing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    stdout = "   NVIDIA GeForce RTX 4090 ,\t 24564 , 18321   \n"
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _smi(stdout))
    gpus = hardware._detect_via_nvidia_smi()
    assert [gpu.to_dict() for gpu in gpus] == [
        {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090",
         "vram_total_mb": 24564, "vram_free_mb": 18321, "source": "nvidia-smi"},
    ]


def test_nvidia_smi_garbage_lines_are_skipped(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    stdout = (
        "NVIDIA GeForce RTX 4090, not-a-number, 18321\n"  # float() raises ValueError
        "only-one-field\n"                                # fewer than three parts
        "\n"                                              # empty line
        "NVIDIA A100-SXM4-80GB, 81920, 70000\n"           # the only valid row
    )
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _smi(stdout))
    gpus = hardware._detect_via_nvidia_smi()
    assert [gpu.to_dict() for gpu in gpus] == [
        {"vendor": "nvidia", "name": "NVIDIA A100-SXM4-80GB",
         "vram_total_mb": 81920, "vram_free_mb": 70000, "source": "nvidia-smi"},
    ]


def test_nvidia_smi_all_garbage(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    stdout = "NVIDIA GeForce RTX 4090, oops, 18321\n"
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _smi(stdout))
    assert hardware._detect_via_nvidia_smi() == []


def test_nvidia_smi_timeout(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert hardware._detect_via_nvidia_smi() == []


def test_nvidia_smi_oserror(monkeypatch):
    def fake_run(argv, **kwargs):
        raise OSError("exec failed")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert hardware._detect_via_nvidia_smi() == []



# --- torch probe ---------------------------------------------------------------


def _install_fake_torch(monkeypatch, available=True, devices=(), hip=None):
    """Point `import torch` at a fake module so the probe is deterministic."""
    fake = types.ModuleType("torch")
    monkeypatch.setattr(fake, "cuda", SimpleNamespace(
        is_available=lambda: available,
        device_count=lambda: len(devices),
        get_device_properties=lambda index: devices[index],
    ), raising=False)
    monkeypatch.setattr(fake, "version", SimpleNamespace(hip=hip), raising=False)
    monkeypatch.setitem(sys.modules, "torch", fake)


def test_detect_via_torch_not_installed(monkeypatch):
    # A None entry in sys.modules makes `import torch` raise ImportError.
    monkeypatch.setitem(sys.modules, "torch", None)
    assert hardware._detect_via_torch() == []


def test_detect_via_torch_cuda_unavailable(monkeypatch):
    _install_fake_torch(monkeypatch, available=False)
    assert hardware._detect_via_torch() == []


def test_detect_via_torch_two_nvidia_devices(monkeypatch):
    devices = [
        SimpleNamespace(name="NVIDIA GeForce RTX 4090", total_memory=24 * 1024 ** 3),
        SimpleNamespace(name="NVIDIA A100-SXM4-80GB", total_memory=80 * 1024 ** 3),
    ]
    _install_fake_torch(monkeypatch, available=True, devices=devices)
    gpus = hardware._detect_via_torch()
    assert [gpu.to_dict() for gpu in gpus] == [
        {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4090",
         "vram_total_mb": 24576, "vram_free_mb": None, "source": "torch"},
        {"vendor": "nvidia", "name": "NVIDIA A100-SXM4-80GB",
         "vram_total_mb": 81920, "vram_free_mb": None, "source": "torch"},
    ]


def test_detect_via_torch_hip_reports_amd(monkeypatch):
    # No name attribute on the props: the probe must fall back to "CUDA device".
    _install_fake_torch(
        monkeypatch, available=True,
        devices=[SimpleNamespace(total_memory=16 * 1024 ** 3)], hip="6.2",
    )
    gpus = hardware._detect_via_torch()
    assert [gpu.to_dict() for gpu in gpus] == [
        {"vendor": "amd", "name": "CUDA device",
         "vram_total_mb": 16384, "vram_free_mb": None, "source": "torch"},
    ]


# --- detect_gpus fallback chain -------------------------------------------------


def test_detect_gpus_prefers_comfyui(monkeypatch):
    comfy = [hardware.GPUInfo(vendor="nvidia", name="RTX 4090", source="comfyui")]
    monkeypatch.setattr(hardware, "_detect_via_comfyui", lambda url: comfy)

    def no_smi():
        raise AssertionError("nvidia-smi must not be probed when ComfyUI answered")

    monkeypatch.setattr(hardware, "_detect_via_nvidia_smi", no_smi)
    assert hardware.detect_gpus("http://127.0.0.1:8188") is comfy


def test_detect_gpus_falls_back_to_nvidia_smi(monkeypatch):
    monkeypatch.setattr(hardware, "_detect_via_comfyui", lambda url: [])
    smi = [hardware.GPUInfo(vendor="nvidia", name="RTX 4090", source="nvidia-smi")]
    monkeypatch.setattr(hardware, "_detect_via_nvidia_smi", lambda: smi)

    def no_torch():
        raise AssertionError("torch must not be probed when nvidia-smi answered")

    monkeypatch.setattr(hardware, "_detect_via_torch", no_torch)
    assert hardware.detect_gpus(None) is smi


def test_detect_gpus_falls_back_to_torch(monkeypatch):
    monkeypatch.setattr(hardware, "_detect_via_nvidia_smi", list)
    torch = [hardware.GPUInfo(vendor="nvidia", name="RTX 4090", source="torch")]
    monkeypatch.setattr(hardware, "_detect_via_torch", lambda: torch)
    assert hardware.detect_gpus(None) is torch


# --- CPU ------------------------------------------------------------------------


def test_detect_cpu_reads_platform(monkeypatch):
    monkeypatch.setattr(platform, "processor", lambda: "Intel64 Family 6 Model 183")
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert hardware._detect_cpu() == ("Intel64 Family 6 Model 183", 16)


def test_detect_cpu_empty_processor_name(monkeypatch):
    monkeypatch.setattr(platform, "processor", lambda: "")
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert hardware._detect_cpu() == (None, 8)


def test_detect_cpu_failure_returns_none(monkeypatch):
    def boom():
        raise RuntimeError("no processor info")

    monkeypatch.setattr(platform, "processor", boom)
    assert hardware._detect_cpu() == (None, None)



# --- RAM ------------------------------------------------------------------------


class _FakeMemPath:
    """Stands in for pathlib.Path so /proc/meminfo can be faked on any OS."""

    def __init__(self, text):
        self._text = text

    def open(self, encoding=None):
        return io.StringIO(self._text)


def _patch_meminfo(monkeypatch, text):
    monkeypatch.setattr(hardware, "Path", lambda path: _FakeMemPath(text))


def test_detect_ram_uses_psutil_when_available(monkeypatch):
    fake = types.ModuleType("psutil")
    monkeypatch.setattr(
        fake, "virtual_memory",
        lambda: SimpleNamespace(total=16 * 1024 ** 3, available=8 * 1024 ** 3),
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert hardware._detect_ram() == (16384, 8192)


def test_detect_ram_psutil_error_falls_back_to_meminfo(monkeypatch):
    def broken():
        raise RuntimeError("cgroup denied")

    fake = types.ModuleType("psutil")
    monkeypatch.setattr(fake, "virtual_memory", broken, raising=False)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    _patch_meminfo(
        monkeypatch,
        "MemTotal:       16384000 kB\n"
        "MemFree:         2048000 kB\n"
        "MemAvailable:    8192000 kB\n",
    )
    assert hardware._detect_ram() == (16000, 8000)


def test_detect_ram_without_psutil_reads_meminfo(monkeypatch):
    # A None entry in sys.modules makes `import psutil` raise ImportError.
    monkeypatch.setitem(sys.modules, "psutil", None)
    _patch_meminfo(
        monkeypatch,
        "MemTotal:       16384000 kB\n"
        "MemFree:         2048000 kB\n"
        "MemAvailable:    8192000 kB\n",
    )
    assert hardware._detect_ram() == (16000, 8000)


def test_detect_ram_meminfo_without_available_uses_free(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    _patch_meminfo(
        monkeypatch,
        "MemTotal:       16384000 kB\n"
        "MemFree:         2048000 kB\n",
    )
    assert hardware._detect_ram() == (16000, 2000)


def test_detect_ram_meminfo_without_any_available(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    _patch_meminfo(monkeypatch, "MemTotal:       16384000 kB\n")
    assert hardware._detect_ram() == (16000, None)


def test_detect_ram_meminfo_garbage_returns_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    _patch_meminfo(monkeypatch, "MemTotal: not-a-number kB\n")
    assert hardware._detect_ram() == (None, None)


def test_detect_ram_meminfo_empty_value_returns_none(monkeypatch):
    # split() on the empty rest raises IndexError; the probe must swallow it.
    monkeypatch.setitem(sys.modules, "psutil", None)
    _patch_meminfo(monkeypatch, "MemTotal:\n")
    assert hardware._detect_ram() == (None, None)


def test_detect_ram_no_psutil_and_no_memfile(monkeypatch):
    class _Missing:
        def open(self, encoding=None):
            raise FileNotFoundError("no /proc on this OS")

    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(hardware, "Path", lambda path: _Missing())
    assert hardware._detect_ram() == (None, None)



# --- disk -----------------------------------------------------------------------


def test_detect_disk_free_gb_rounds_to_one_decimal(monkeypatch):
    calls = {}

    def fake_usage(path):
        calls["path"] = path
        return SimpleNamespace(free=int(5.5 * 1024 ** 3))

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    assert hardware.detect_disk_free_gb("/tmp") == 5.5
    assert calls["path"] == "/tmp"


def test_detect_disk_free_gb_defaults_to_cwd(monkeypatch):
    calls = {}

    def fake_usage(path):
        calls["path"] = path
        return SimpleNamespace(free=1024 ** 3)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    assert hardware.detect_disk_free_gb() == 1.0
    assert isinstance(calls["path"], Path)


def test_detect_disk_free_gb_oserror_returns_none(monkeypatch):
    def broken(path):
        raise OSError("no such filesystem")

    monkeypatch.setattr(shutil, "disk_usage", broken)
    assert hardware.detect_disk_free_gb("/does/not/exist") is None


# --- detect_hardware ------------------------------------------------------------


def test_detect_hardware_assembles_snapshot(monkeypatch):
    from app.core.config import settings

    gpu = hardware.GPUInfo(vendor="nvidia", name="RTX 4090", vram_total_mb=24564)
    seen = {}

    def fake_gpus(url):
        seen["url"] = url
        return [gpu]

    monkeypatch.setattr(hardware, "detect_gpus", fake_gpus)
    monkeypatch.setattr(hardware, "_detect_cpu", lambda: ("CPU X", 8))
    monkeypatch.setattr(hardware, "_detect_ram", lambda: (16384, 8192))
    monkeypatch.setattr(hardware, "detect_disk_free_gb", lambda path=None: 500.0)
    monkeypatch.setattr(settings, "comfyui_base_url", "")

    info = hardware.detect_hardware()
    assert seen["url"] == ""  # settings value used when no explicit url is passed
    assert info.gpus == [gpu]
    assert info.cpu_name == "CPU X"
    assert info.cpu_cores == 8
    assert info.ram_total_mb == 16384
    assert info.ram_available_mb == 8192
    assert info.disk_free_gb == 500.0
    assert info.cuda_available is True
    assert info.rocm_available is False


def test_detect_hardware_explicit_url_wins_over_settings(monkeypatch):
    from app.core.config import settings

    seen: dict[str, str] = {}

    def fake_gpus(url):
        seen["url"] = url
        return []

    monkeypatch.setattr(hardware, "detect_gpus", fake_gpus)
    monkeypatch.setattr(settings, "comfyui_base_url", "http://settings-host:8188")
    hardware.detect_hardware("http://explicit:8188")
    assert seen["url"] == "http://explicit:8188"


def test_detect_hardware_amd_gpu_sets_rocm_flag(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(
        hardware, "detect_gpus",
        lambda url: [hardware.GPUInfo(vendor="amd", name="RX 7900 XTX")],
    )
    monkeypatch.setattr(settings, "comfyui_base_url", "")
    info = hardware.detect_hardware()
    assert info.cuda_available is False
    assert info.rocm_available is True





