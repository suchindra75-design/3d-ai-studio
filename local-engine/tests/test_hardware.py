import platform
from pathlib import Path

import pytest

from ai3d_engine import hardware, runtime
from ai3d_engine.hardware import GPUInfo, HardwareReport, detect_hardware
from ai3d_engine.runtime import find_tool, run_tool


def _report(**overrides: object) -> HardwareReport:
    base: dict[str, object] = {
        "machine_id": "m",
        "hostname": "h",
        "os_name": "Linux",
        "os_version": "test",
        "arch": "x86_64",
        "cpu_model": "cpu",
        "cpu_cores_physical": 4,
        "cpu_cores_logical": 8,
        "ram_total_bytes": 32 * 1024**3,
        "disk_free_bytes": 100 * 1024**3,
        "accelerators": {"cuda": {"available": False}},
        "dependencies": {"python": {}},
    }
    base.update(overrides)
    return HardwareReport(**base)  # type: ignore[arg-type]


def test_detect_hardware_on_this_machine() -> None:
    report = detect_hardware()
    assert report.cpu_cores_logical is not None
    assert report.cpu_cores_logical >= 1
    assert report.ram_total_bytes is not None
    assert report.ram_total_bytes > 0
    assert report.arch == platform.machine()
    assert set(report.capabilities) >= {"local_ai_generation", "photogrammetry_sparse"}
    report.to_dict()  # JSON-friendly


def test_tools_outside_allow_list_are_refused() -> None:
    with pytest.raises(ValueError, match="allow-list"):
        find_tool("sh")
    with pytest.raises(ValueError, match="allow-list"):
        run_tool("rm", ["-rf", "/"])


def test_well_known_locations_outside_path_are_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Apps started from Finder get a minimal PATH; the documented install still counts.
    colmap = tmp_path / ".local/share/ai3d-studio/tools/colmap/bin/colmap"
    colmap.parent.mkdir(parents=True)
    colmap.write_text("")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(runtime, "_CONFIGURED", {})
    assert find_tool("colmap") == colmap


@pytest.mark.parametrize(
    ("text", "expected"), [("4 GB", 4096), ("1536 MB", 1536), ("garbage", None), (None, None)]
)
def test_parse_vram(text: str | None, expected: int | None) -> None:
    assert hardware._parse_vram_mb(text) == expected


def test_cuda_gpu_with_enough_vram_supports_local_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    report = _report(
        gpus=[GPUInfo(name="RTX", vendor="NVIDIA", vram_mb=12288, cuda=True)],
        accelerators={"cuda": {"available": True}},
        dependencies={"colmap": {"installed": True}, "python": {}},
    )
    caps = hardware._capabilities(report)
    assert caps["local_ai_generation"].supported
    assert caps["photogrammetry_dense"].supported


def test_intel_mac_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    report = _report(
        os_name="Darwin",
        arch="x86_64",
        gpus=[GPUInfo(name="AMD Radeon Pro 5300M", vendor="AMD", vram_mb=4096, metal="metal3")],
        dependencies={"colmap": {"installed": True}, "python": {}},
    )
    caps = hardware._capabilities(report)
    assert not caps["local_ai_generation"].supported
    assert "Intel Mac" in caps["local_ai_generation"].reason
    assert caps["photogrammetry_sparse"].supported
    assert not caps["photogrammetry_dense"].supported


def test_low_disk_is_flagged() -> None:
    caps = hardware._capabilities(_report(disk_free_bytes=1024**3))
    assert not caps["disk_space"].supported
