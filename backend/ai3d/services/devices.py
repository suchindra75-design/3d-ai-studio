"""Local device detection, persisted to ``local_devices``."""

import threading
import time
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai3d.core.timeutil import utcnow
from ai3d.models import LocalDevice
from ai3d_engine.hardware import HardwareReport, detect_hardware

_CACHE_TTL_SECONDS = 60.0
_cache_lock = threading.Lock()
_cache: tuple[float, HardwareReport] | None = None


def _detect_cached(data_dir: Path, *, refresh: bool) -> HardwareReport:
    """Detection shells out to system tools (~1 s), so results are cached briefly."""
    global _cache  # noqa: PLW0603
    with _cache_lock:
        now = time.monotonic()
        if refresh or _cache is None or now - _cache[0] > _CACHE_TTL_SECONDS:
            _cache = (now, detect_hardware(data_dir))
        return _cache[1]


def cuda_available(data_dir: Path) -> bool:
    """Whether a CUDA GPU was detected (cached; cheap to call while planning jobs)."""
    return bool(
        _detect_cached(data_dir, refresh=False).accelerators.get("cuda", {}).get("available")
    )


def detect_local_device(db: Session, data_dir: Path, *, refresh: bool = False) -> LocalDevice:
    report = _detect_cached(data_dir, refresh=refresh)
    device = db.scalar(select(LocalDevice).where(LocalDevice.machine_id == report.machine_id))
    if device is None:
        device = LocalDevice(machine_id=report.machine_id, last_seen_at=utcnow())
        db.add(device)
    device.hostname = report.hostname
    device.os_name = report.os_name
    device.os_version = report.os_version
    device.arch = report.arch
    device.cpu_model = report.cpu_model
    device.cpu_cores_physical = report.cpu_cores_physical
    device.cpu_cores_logical = report.cpu_cores_logical
    device.ram_total_bytes = report.ram_total_bytes
    device.disk_free_bytes = report.disk_free_bytes
    device.gpus = [asdict(g) for g in report.gpus]
    device.accelerators = report.accelerators
    device.dependencies = report.dependencies
    device.capabilities = {k: asdict(v) for k, v in report.capabilities.items()}
    device.last_seen_at = utcnow()
    db.commit()
    return device
