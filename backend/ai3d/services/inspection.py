"""Model health inspection (D69): run in the background, in a separate process (the
checks hold Python's lock for long stretches, as level building did; D65), and
kept as a report file with the version."""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import NotFoundError
from ai3d.core.timeutil import utcnow
from ai3d.database.session import Database
from ai3d.models import Asset, ModelVersion
from ai3d.models.enums import AssetKind
from ai3d.processing import inspect
from ai3d.processing.mesh import GlbCounts, glb_counts, memory_shortfall
from ai3d.schemas.inspection import InspectionRead, InspectionReportRead, InspectionRequest
from ai3d.services.models import get_model, get_version, version_file
from ai3d.storage import StorageBackend
from ai3d.storage.keys import model_asset_key

log = logging.getLogger(__name__)
_API = "/api/v1"
REPORT_TYPE = "inspection"
# Peak memory of an inspection (docs/BENCHMARKS.md#inspection, 2026-11-08): up to
# 4.9 GB on the 10M-triangle Statuette with the printing checks, 2.2 GB on a textured
# 5M terrain. Vertices count separately (welding and duplicate checks are per
# vertex). The estimate stays above every measured run (test_memory_estimates.py).
BASE_BYTES = 700 * 1024 * 1024
BYTES_PER_TRIANGLE = 350
BYTES_PER_VERTEX = 300

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inspection")
_running: dict[uuid.UUID, Future[None]] = {}
_failures: dict[uuid.UUID, str] = {}
_lock = threading.Lock()


def memory_bytes(counts: GlbCounts) -> int:
    return (
        BASE_BYTES
        + counts.triangles * BYTES_PER_TRIANGLE
        + counts.vertices * BYTES_PER_VERTEX
        + counts.texture_bytes
    )


def _asset(db: Session, version_id: uuid.UUID) -> Asset | None:
    for asset in db.scalars(
        select(Asset).where(Asset.model_version_id == version_id, Asset.kind == AssetKind.REPORT)
    ):
        if (asset.meta or {}).get("type") == REPORT_TYPE:
            return asset
    return None


def read_report(db: Session, storage: StorageBackend, version_id: uuid.UUID) -> dict[str, Any]:
    asset = _asset(db, version_id)
    if asset is None:
        raise NotFoundError("This version has not been inspected yet.")
    with storage.open(asset.storage_key) as handle:
        report: dict[str, Any] = json.load(handle)
    return report


def to_read(db: Session, storage: StorageBackend, version: ModelVersion) -> InspectionRead:
    base = f"{_API}/model-versions/{version.id}/inspection"
    if version.id in _running:
        return InspectionRead(version_id=version.id, status="running")
    asset = _asset(db, version.id)
    if version.id in _failures and asset is None:
        return InspectionRead(version_id=version.id, status="failed", message=_failures[version.id])
    if asset is None:
        return InspectionRead(version_id=version.id, status="missing")
    report = read_report(db, storage, version.id)
    return InspectionRead(
        version_id=version.id,
        status="ready",
        report=InspectionReportRead.model_validate(report),
        created_at=asset.created_at,
        json_url=f"{base}/report.json",
        markdown_url=f"{base}/report.md",
        message=_failures.get(version.id),
    )


def get_inspection(db: Session, storage: StorageBackend, version_id: uuid.UUID) -> InspectionRead:
    return to_read(db, storage, get_version(db, version_id))


def start(
    database: Database,
    storage: StorageBackend,
    settings: Settings,
    version_id: uuid.UUID,
    body: InspectionRequest,
) -> InspectionRead:
    """Start inspecting in the background (one at a time); returns at once. A report
    already there stays until the new one replaces it."""
    from ai3d.core.errors import InvalidInputError  # noqa: PLC0415

    with _lock, database.session() as db:
        version = get_version(db, version_id)
        if version.id in _running:
            return to_read(db, storage, version)
        _, mesh_asset = version_file(db, version.id, AssetKind.MESH)
        local = storage.local_path(mesh_asset.storage_key)
        counts = (glb_counts(local) if local is not None else None) or GlbCounts.from_stats(
            version.stats
        )
        short = memory_shortfall(memory_bytes(counts), f"Inspecting {counts.triangles:,} triangles")
        if short:
            raise InvalidInputError(short, code="not_enough_memory")
        options = body.model_dump()
        # Models with a real-world scale are in metres (glTF): 0.001 m per mm.
        options["units_per_mm"] = 0.001 if (version.stats or {}).get("scale") else None
        _failures.pop(version.id, None)
        _running[version.id] = _executor.submit(
            _run, database, storage, settings, version.id, options
        )
        return InspectionRead(version_id=version.id, status="running")


def _run(
    database: Database,
    storage: StorageBackend,
    settings: Settings,
    version_id: uuid.UUID,
    options: dict[str, Any],
) -> None:
    try:
        with database.session() as db:
            _inspect(db, storage, settings, version_id, options)
    except Exception as exc:
        log.exception("inspection failed", extra={"version_id": str(version_id)})
        _failures[version_id] = (
            f"The inspection failed ({type(exc).__name__}); the model is unchanged. Try again."
        )
    finally:
        _running.pop(version_id, None)


def _inspect_in_process(path: Path, options: dict[str, Any]) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
        return pool.submit(inspect.inspect_file, str(path), options).result()


def _inspect(
    db: Session,
    storage: StorageBackend,
    settings: Settings,
    version_id: uuid.UUID,
    options: dict[str, Any],
) -> None:
    version, mesh_asset = version_file(db, version_id, AssetKind.MESH)
    model = get_model(db, version.model_id)
    tmp_root = settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="inspection-", dir=tmp_root))
    try:
        source = workdir / "source.glb"
        with storage.open(mesh_asset.storage_key) as src, source.open("wb") as copy:
            shutil.copyfileobj(src, copy)
        report = _inspect_in_process(source, options)
        data = json.dumps(report).encode()
        key = model_asset_key(
            model.project_id,
            model.id,
            version.version_number,
            f"inspection/report-{utcnow().strftime('%Y%m%dT%H%M%S%f')}.json",
        )
        storage.save_bytes(key, data)
        old = _asset(db, version.id)
        db.add(
            Asset(
                project_id=model.project_id,
                model_version_id=version.id,
                kind=AssetKind.REPORT,
                storage_key=key,
                mime_type="application/json",
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                meta={"type": REPORT_TYPE, "options": options},
            )
        )
        if old is not None:  # the new report replaces it
            db.delete(old)
        db.commit()
        if old is not None:
            storage.delete(old.storage_key)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def markdown(db: Session, storage: StorageBackend, version_id: uuid.UUID) -> str:
    version = get_version(db, version_id)
    model = get_model(db, version.model_id)
    report = read_report(db, storage, version_id)
    return inspect.markdown(report, f"{model.name}, version {version.version_number}")
