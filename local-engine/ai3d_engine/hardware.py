"""Hardware and dependency detection for local processing.

Produces a JSON-serialisable report used to (a) show users what their machine
can do and (b) let the AUTOMATIC processing mode pick a route. Every check
degrades to "unknown"/"not found" instead of raising.

Capability verdicts are deliberately conservative:
* CUDA is only reported when ``nvidia-smi`` (or an installed PyTorch) confirms it.
* Apple MPS requires Apple Silicon; Intel Macs have no supported PyTorch GPU path.
* COLMAP dense stereo (PatchMatch) needs CUDA, so it is never offered on macOS.
"""

import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import re
import shutil
import socket
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

from ai3d_engine.runtime import find_tool, run_tool

# Python packages relevant to reconstruction, checked without importing them.
_PY_PACKAGES = ("torch", "open3d", "trimesh", "pycolmap", "opencv-python-headless", "bpy")

# TripoSR at High quality peaked at 3.7 GB of memory on the CPU (measured); 8 GB of
# RAM leaves room for the app and the OS.
MIN_RAM_LOCAL_AI = 8 * 1024**3
MIN_VRAM_LOCAL_AI_MB = 6 * 1024  # smallest documented requirement (TripoSR/SF3D ≈ 6 GB)
MIN_FREE_DISK = 5 * 1024**3


@dataclass(slots=True)
class GPUInfo:
    name: str
    vendor: str
    vram_mb: int | None = None
    driver: str | None = None
    metal: str | None = None
    cuda: bool = False


@dataclass(slots=True)
class Capability:
    supported: bool
    reason: str


@dataclass(slots=True)
class HardwareReport:
    machine_id: str
    hostname: str
    os_name: str
    os_version: str
    arch: str
    cpu_model: str
    cpu_cores_physical: int | None
    cpu_cores_logical: int | None
    ram_total_bytes: int | None
    disk_free_bytes: int | None
    gpus: list[GPUInfo] = field(default_factory=list)
    accelerators: dict[str, Any] = field(default_factory=dict)
    dependencies: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Capability] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _machine_id() -> str:
    raw = f"{socket.gethostname()}-{uuid.getnode():012x}-{platform.machine()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _os_version() -> str:
    system = platform.system()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    if system == "Windows":
        return f"Windows {platform.version()}"
    try:
        info = platform.freedesktop_os_release()
        return info.get("PRETTY_NAME", platform.release())
    except OSError:
        return platform.release()


def _cpu_model() -> str:
    system = platform.system()
    if system == "Darwin":
        out = run_tool("sysctl", ["-n", "machdep.cpu.brand_string"])
        if out and out.strip():
            return out.strip()
    elif system == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "unknown"


def _parse_vram_mb(text: str | None) -> int | None:
    if not text:
        return None
    match = re.match(r"\s*([\d.]+)\s*(GB|MB)", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    return int(value * 1024 if match.group(2).upper() == "GB" else value)


def _macos_gpus() -> list[GPUInfo]:
    out = run_tool("system_profiler", ["SPDisplaysDataType", "-json"], timeout=20)
    if not out:
        return []
    try:
        entries = json.loads(out).get("SPDisplaysDataType", [])
    except json.JSONDecodeError:
        return []
    gpus = []
    for entry in entries:
        vendor_raw = str(entry.get("spdisplays_vendor", "")).lower()
        name = str(entry.get("sppci_model") or entry.get("_name") or "GPU")
        if "apple" in vendor_raw or name.startswith("Apple"):
            vendor = "Apple"
        elif "amd" in vendor_raw or "ati" in vendor_raw or "0x1002" in vendor_raw:
            vendor = "AMD"
        elif "intel" in vendor_raw:
            vendor = "Intel"
        elif "nvidia" in vendor_raw:
            vendor = "NVIDIA"
        else:
            vendor = "unknown"
        vram = entry.get("spdisplays_vram") or entry.get("spdisplays_vram_shared")
        metal = entry.get("spdisplays_mtlgpufamilysupport") or entry.get("spdisplays_metal")
        gpus.append(
            GPUInfo(
                name=name,
                vendor=vendor,
                vram_mb=_parse_vram_mb(str(vram) if vram else None),
                metal=str(metal).replace("spdisplays_", "") if metal else None,
            )
        )
    return gpus


def _nvidia_gpus() -> list[GPUInfo]:
    out = run_tool(
        "nvidia-smi",
        ["--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
    )
    if not out:
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            vram = int(float(parts[1]))
        except ValueError:
            vram = None
        gpus.append(
            GPUInfo(name=parts[0], vendor="NVIDIA", vram_mb=vram, driver=parts[2], cuda=True)
        )
    return gpus


def _linux_other_gpus() -> list[GPUInfo]:
    out = run_tool("lspci", [])
    if not out:
        return []
    gpus = []
    for line in out.splitlines():
        if re.search(r"VGA compatible controller|3D controller|Display controller", line):
            name = line.split(":", 2)[-1].strip()
            if "NVIDIA" in name:
                continue  # covered by nvidia-smi when the driver is installed
            vendor = (
                "AMD"
                if ("AMD" in name or "ATI" in name)
                else ("Intel" if "Intel" in name else "unknown")
            )
            gpus.append(GPUInfo(name=name, vendor=vendor))
    return gpus


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _tool_version(name: str, args: list[str], pattern: str) -> dict[str, Any]:
    path = find_tool(name)
    if path is None:
        return {"installed": False}
    out = run_tool(name, args, timeout=20) or ""
    match = re.search(pattern, out)
    return {"installed": True, "path": str(path), "version": match.group(1) if match else None}


def _torch_accelerators() -> dict[str, Any]:
    """Only imports torch if it is installed (importing it takes seconds)."""
    if importlib.util.find_spec("torch") is None:
        return {"installed": False}
    try:
        import torch  # type: ignore[import-not-found]  # noqa: PLC0415

        mps = getattr(torch.backends, "mps", None)
        return {
            "installed": True,
            "version": torch.__version__,
            "cuda": bool(torch.cuda.is_available()),
            "mps": bool(mps and mps.is_available()),
        }
    except Exception as exc:  # broken installs must not break detection
        return {"installed": True, "error": str(exc)[:200]}


def _openmvs_dependency() -> dict[str, Any]:
    from ai3d_engine.reconstruction.openmvs import find_openmvs  # noqa: PLC0415

    folder = find_openmvs()
    return {"installed": folder is not None, "path": str(folder) if folder else None}


def _capabilities(report: HardwareReport) -> dict[str, Capability]:
    system = platform.system()
    arch = report.arch.lower()
    cuda = bool(report.accelerators.get("cuda", {}).get("available"))
    mps_possible = system == "Darwin" and arch in {"arm64", "aarch64"}
    best_vram = max((g.vram_mb or 0 for g in report.gpus if g.cuda), default=0)
    ram = report.ram_total_bytes or 0
    disk = report.disk_free_bytes or 0
    colmap = report.dependencies.get("colmap", {})
    pycolmap = report.dependencies.get("python", {}).get("pycolmap")

    caps: dict[str, Capability] = {}

    cpu_note = (
        "Local AI (TripoSR) runs on the CPU: about 1.5 minutes per photo on a 6-core "
        "Intel Mac, longer on fewer cores."
    )
    if cuda and best_vram >= MIN_VRAM_LOCAL_AI_MB:
        caps["local_ai_generation"] = Capability(
            True, f"NVIDIA GPU with {best_vram // 1024} GB VRAM and CUDA detected (fastest)."
        )
    elif ram >= MIN_RAM_LOCAL_AI:
        gpu_note = ""
        if cuda:
            gpu_note = f" The CUDA GPU has {best_vram} MB VRAM; models need ≥ 6 GB on the GPU."
        elif mps_possible:
            gpu_note = " Apple's GPU (MPS) is not used."
        caps["local_ai_generation"] = Capability(True, cpu_note + gpu_note)
    else:
        caps["local_ai_generation"] = Capability(
            False,
            f"{ram / 1024**3:.0f} GB of RAM; local AI generation needs at least "
            f"{MIN_RAM_LOCAL_AI // 1024**3} GB.",
        )

    if colmap.get("installed") or pycolmap:
        caps["photogrammetry_sparse"] = Capability(
            True, "COLMAP is available for feature matching and camera pose estimation (CPU)."
        )
    else:
        caps["photogrammetry_sparse"] = Capability(
            False, "COLMAP is not installed. See docs/LOCAL_GPU_SETUP.md."
        )

    openmvs = report.dependencies.get("openmvs", {})
    if openmvs.get("installed") and (colmap.get("installed") or pycolmap):
        caps["photogrammetry_dense"] = Capability(
            True,
            "OpenMVS computes dense surfaces on the CPU (about 7 minutes for 67 photos on a "
            "6-core computer).",
        )
    elif cuda and (colmap.get("installed") or pycolmap):
        caps["photogrammetry_dense"] = Capability(True, "COLMAP dense stereo can use the CUDA GPU.")
    else:
        caps["photogrammetry_dense"] = Capability(
            False,
            "Dense surfaces need OpenMVS (CPU; see docs/LOCAL_GPU_SETUP.md) or an NVIDIA GPU "
            "with CUDA. Without them the surface comes from sparse points, which is coarse on "
            "real photos.",
        )

    if disk < MIN_FREE_DISK:
        caps["disk_space"] = Capability(
            False, f"Only {disk / 1024**3:.1f} GB free; at least 5 GB is recommended."
        )
    else:
        caps["disk_space"] = Capability(True, f"{disk / 1024**3:.0f} GB free.")
    return caps


def detect_hardware(data_dir: Path | None = None) -> HardwareReport:
    system = platform.system()
    vm = psutil.virtual_memory()
    disk_target = data_dir if data_dir and data_dir.exists() else Path.home()
    try:
        disk_free = shutil.disk_usage(disk_target).free
    except OSError:
        disk_free = None

    gpus = _nvidia_gpus()
    if system == "Darwin":
        gpus = _macos_gpus() + gpus
    elif system == "Linux":
        gpus = gpus + _linux_other_gpus()

    torch_info = _torch_accelerators()
    cuda_gpu = any(g.cuda for g in gpus)
    arch = platform.machine()
    metal_gpus = [g.name for g in gpus if g.metal]

    accelerators: dict[str, Any] = {
        "cuda": {
            "available": cuda_gpu or bool(torch_info.get("cuda")),
            "source": "nvidia-smi" if cuda_gpu else ("torch" if torch_info.get("cuda") else None),
        },
        "metal": {"available": bool(metal_gpus), "gpus": metal_gpus},
        "mps": {
            "possible": system == "Darwin" and arch.lower() in {"arm64", "aarch64"},
            "available": bool(torch_info.get("mps")),
        },
        "torch": torch_info,
    }

    dependencies: dict[str, Any] = {
        "colmap": _tool_version("colmap", ["-h"], r"COLMAP\s+([\d.]+\w*)"),
        "blender": _tool_version("blender", ["--version"], r"Blender\s+([\d.]+)"),
        "openmvs": _openmvs_dependency(),
        "python": {name: _package_version(name) for name in _PY_PACKAGES},
    }

    report = HardwareReport(
        machine_id=_machine_id(),
        hostname=socket.gethostname(),
        os_name=system or "unknown",
        os_version=_os_version(),
        arch=arch,
        cpu_model=_cpu_model(),
        cpu_cores_physical=psutil.cpu_count(logical=False),
        cpu_cores_logical=psutil.cpu_count(logical=True),
        ram_total_bytes=int(vm.total),
        disk_free_bytes=disk_free,
        gpus=gpus,
        accelerators=accelerators,
        dependencies=dependencies,
    )
    report.capabilities = _capabilities(report)
    return report
