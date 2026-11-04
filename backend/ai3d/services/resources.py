"""Can this computer run a heavy local job right now? Memory and disk, before it starts.

The needs are measured, rounded up (6-core Intel Mac, macOS 26):

* Local AI (TripoSR, High quality): peak 3.7 GB of memory; a few MB on disk.
* Photogrammetry: a real 67-photo capture (540 MB of photos) used an 876 MB
  workspace with OpenMVS (977 MB with the texture); COLMAP and OpenMVS steps
  peaked at 0.7–1.9 GB (texturing is the largest).

Too little disk blocks the job (it would fail half-way). Too little *installed*
memory for the local AI engine makes Automatic mode use the cloud instead, and
Local mode explain why it cannot run. Too little *free* memory only warns:
closing other programs is enough.
"""

from dataclasses import dataclass
from pathlib import Path

import psutil

GB = 1024**3


@dataclass(frozen=True, slots=True)
class Needs:
    disk_bytes: int
    free_memory_bytes: int  # wanted free when the job starts
    installed_memory_bytes: int = 0  # below this the engine is not offered at all


LOCAL_AI = Needs(disk_bytes=1 * GB, free_memory_bytes=int(4.5 * GB), installed_memory_bytes=8 * GB)


def photogrammetry(photo_bytes: int) -> Needs:
    """Undistorted copies, depth maps and the textured surface: about 3× the photos."""
    return Needs(disk_bytes=max(2 * GB, 3 * photo_bytes), free_memory_bytes=4 * GB)


@dataclass(frozen=True, slots=True)
class Machine:
    disk_free_bytes: int
    memory_total_bytes: int
    memory_available_bytes: int


def machine(data_dir: Path) -> Machine:
    folder = data_dir if data_dir.exists() else data_dir.parent
    memory = psutil.virtual_memory()
    return Machine(
        disk_free_bytes=psutil.disk_usage(str(folder)).free,
        memory_total_bytes=int(memory.total),
        memory_available_bytes=int(memory.available),
    )


def _gb(value: int) -> str:
    return f"{value / GB:.1f} GB"


def too_small(needs: Needs, here: Machine, engine: str) -> str | None:
    """Why the engine cannot run on this computer at all, or None."""
    if needs.installed_memory_bytes and here.memory_total_bytes < needs.installed_memory_bytes:
        return (
            f"{engine} needs a computer with at least {_gb(needs.installed_memory_bytes)} of "
            f"memory; this one has {_gb(here.memory_total_bytes)}."
        )
    return None


def not_enough_disk(needs: Needs, here: Machine, engine: str) -> str | None:
    if here.disk_free_bytes < needs.disk_bytes:
        return (
            f"{engine} needs about {_gb(needs.disk_bytes)} of free disk space in the data "
            f"folder; {_gb(here.disk_free_bytes)} is free. Free up space and try again."
        )
    return None


def low_memory(needs: Needs, here: Machine, engine: str) -> str | None:
    if here.memory_available_bytes < needs.free_memory_bytes:
        return (
            f"Only {_gb(here.memory_available_bytes)} of memory is free and {engine} uses up to "
            f"about {_gb(needs.free_memory_bytes)}: close other programs, or it may be slow or "
            "fail."
        )
    return None
