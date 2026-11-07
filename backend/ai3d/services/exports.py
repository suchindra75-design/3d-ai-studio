"""Exports of a model version: convert, re-open to validate, store, list."""

import gc
import hashlib
import io
import logging
import shutil
import socket
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import trimesh
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import InvalidInputError, NotFoundError
from ai3d.core.timeutil import utcnow
from ai3d.models import Asset, Export, ModelVersion
from ai3d.models.enums import AssetKind, ExportFormat, ExportStatus
from ai3d.processing import exporters, quads
from ai3d.processing.mesh import GlbCounts, glb_counts, load_model_scene, memory_shortfall
from ai3d.schemas.export import ExportFormatRead, ExportRead
from ai3d.services.models import download_filename, get_model, get_version, version_file
from ai3d.storage import StorageBackend
from ai3d.storage.keys import model_asset_key

log = logging.getLogger(__name__)


def list_formats() -> list[ExportFormatRead]:
    return [
        ExportFormatRead(
            format=info.format,
            label=info.label,
            available=info.available,
            unavailable_reason=info.reason,
            keeps_texture=info.keeps_texture,
            keeps_colour=info.keeps_colour,
            notes=list(info.notes),
        )
        for info in exporters.formats()
    ]


def _notes(fmt: ExportFormat, version: ModelVersion) -> list[str]:
    scale_known = bool((version.stats or {}).get("scale"))
    notes = list(exporters.format_info(fmt).notes)
    if not scale_known:
        notes.append(
            "The model's real-world scale is unknown, so sizes are in the model's own units"
            + (
                " (written as if they were metres)."
                if fmt in {ExportFormat.STL, ExportFormat.THREE_MF}
                else "."
            )
        )
    return notes


# Peak memory of an export, measured with the whole process tree (docs/BENCHMARKS.md,
# 2026-11-07), estimated from the GLB header; each estimate is above every measured
# run (tests/test_memory_estimates.py).
# OBJ, STL and the rest: up to 5.7 GB for the 10-million-triangle scan (STL), 4.6 GB
# for a textured 5-million-triangle terrain (OBJ writes texture coordinates as text)
# and 4.4 GB for a soup of 5 million triangles with 15 million vertices (OBJ).
EXPORT_BASE_BYTES = 700 * 1024 * 1024
EXPORT_BYTES_PER_TRIANGLE = 600
EXPORT_BYTES_PER_VERTEX = 150
EXPORT_BYTES_PER_TEXTURED_VERTEX = 800  # on top of the above
# GLB is written in binary: 1.7 GB for the 10M scan, 4.2 GB for a textured 20M terrain.
GLB_EXPORT_BASE_BYTES = 500 * 1024 * 1024
GLB_EXPORT_BYTES = (100, 180, 100)  # per triangle, vertex, textured vertex
# FBX: Blender (its own process) needs most of it: 1.9 GB for a textured 1M terrain,
# 7.6 GB for a textured 5M one, up to 8.6 GB for the 10M scan (it varies by about
# 0.7 GB between runs of the same model).
FBX_EXPORT_BASE_BYTES = 1024 * 1024 * 1024
FBX_EXPORT_BYTES = (850, 0, 1100)


def export_memory_bytes(counts: GlbCounts, fmt: ExportFormat | None = None) -> int:
    """Estimated peak memory of exporting a model as ``fmt`` (all formats but GLB and
    FBX share one estimate)."""
    if fmt in {ExportFormat.GLB, ExportFormat.FBX}:
        base, rates = (
            (GLB_EXPORT_BASE_BYTES, GLB_EXPORT_BYTES)
            if fmt == ExportFormat.GLB
            else (FBX_EXPORT_BASE_BYTES, FBX_EXPORT_BYTES)
        )
        per_triangle, per_vertex, per_textured = rates
        return (
            base
            + counts.triangles * per_triangle
            + counts.vertices * per_vertex
            + counts.textured_vertices * per_textured
            + counts.texture_bytes
        )
    return (
        EXPORT_BASE_BYTES
        + counts.triangles * EXPORT_BYTES_PER_TRIANGLE
        + counts.vertices * EXPORT_BYTES_PER_VERTEX
        + counts.textured_vertices * EXPORT_BYTES_PER_TEXTURED_VERTEX
        + counts.texture_bytes
    )


# Formats that can hold quads (the others are triangles only).
QUAD_FORMATS = {ExportFormat.OBJ, ExportFormat.FBX}


def _quad_pairs(
    db: Session, storage: StorageBackend, version_id: uuid.UUID, scene: trimesh.Scene
) -> dict[str, np.ndarray] | None:
    """The version's quad pairs (from retopology) that still match ``scene``'s meshes."""
    asset = db.scalar(
        select(Asset).where(
            Asset.model_version_id == version_id, Asset.kind == AssetKind.QUAD_TOPOLOGY
        )
    )
    if asset is None or not storage.exists(asset.storage_key):
        return None
    with storage.open(asset.storage_key) as handle:
        stored = dict(np.load(io.BytesIO(handle.read())))
    meshes = {n: g for n, g in scene.geometry.items() if isinstance(g, trimesh.Trimesh)}
    if set(stored) - set(meshes) and len(stored) == 1 and len(meshes) == 1:
        # One mesh, renamed on the way through the GLB.
        stored = {next(iter(meshes)): next(iter(stored.values()))}
    checked = {}
    for name, pairs in stored.items():
        if name in meshes:
            kept = quads.check_pairs(meshes[name], pairs)
            if len(kept):
                checked[name] = kept
    return checked or None


def _check_memory(
    storage: StorageBackend, version: ModelVersion, key: str, fmt: ExportFormat
) -> None:
    """Refuse (nothing recorded) when the export would need more memory than is free."""
    local = storage.local_path(key)
    counts = (glb_counts(local) if local is not None else None) or GlbCounts.from_stats(
        version.stats
    )
    short = memory_shortfall(
        export_memory_bytes(counts, fmt),
        f"Writing {counts.triangles:,} triangles as {exporters.format_info(fmt).label}",
    )
    if short:
        raise InvalidInputError(short, code="not_enough_memory")


def _write_checked(
    source: Path,
    fmt: ExportFormat,
    out: Path,
    quad_pairs: Callable[[trimesh.Scene], dict[str, np.ndarray] | None],
) -> tuple[list[str], dict[str, Any]]:
    """Write the file, re-open it, and note quads that could not be kept.

    The model is freed before the written file is re-opened: the check compares the
    re-opened file with facts taken from the model (triangles, size, texture,
    colours), so the two never need to be in memory together."""
    scene = load_model_scene(source)
    facts = exporters.scene_facts(scene)
    pairs = quad_pairs(scene)
    expected = sum(len(p) for p in (pairs or {}).values())
    if fmt == ExportFormat.FBX:
        # Blender gets the model as a GLB written from it (as before: Blender used
        # less memory on these than on the stored file, measured), and the model is
        # freed before Blender starts, so the two are never in memory together.
        handover = out.with_name("fbx-source.glb")
        handover.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
        centres = quads.pair_centres(scene, pairs) if pairs else None
        del scene, pairs
        gc.collect()  # trimesh scenes hold reference cycles
        notes = exporters.write_fbx_from_glb(handover, out, quad_centres=centres)
        handover.unlink()
    else:
        notes = exporters.write(scene, fmt, out, quad_pairs=pairs) or []
        del scene, pairs
        gc.collect()
    validation = exporters.validate(out, fmt, facts, quads_expected=expected)
    if expected and validation["quads"] < expected:
        notes.append(
            f"{validation['quads']:,} of the {expected:,} quads from retopology were "
            "kept; the rest are written as triangles."
        )
    return notes, validation


def create_export(
    db: Session,
    storage: StorageBackend,
    settings: Settings,
    version_id: uuid.UUID,
    fmt: ExportFormat,
) -> Export:
    """Convert the version, re-open the result, and keep it only if it matches."""
    version, mesh_asset = version_file(db, version_id, AssetKind.MESH)
    model = get_model(db, version.model_id)
    info = exporters.format_info(fmt)
    if not info.available:
        raise InvalidInputError(
            info.reason or f"{info.label} export is not available.",
            code="format_unavailable",
        )
    _check_memory(storage, version, mesh_asset.storage_key, fmt)
    tmp_root = settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="export-", dir=tmp_root))
    export = Export(
        project_id=model.project_id,
        model_version_id=version.id,
        format=fmt,
        status=ExportStatus.RUNNING,
        # Who is writing it, and where: if this process stops part-way, the next
        # start finds the export by these and marks it interrupted (see
        # recover_interrupted).
        options={"notes": _notes(fmt, version), "worker": _this_worker(), "workdir": str(workdir)},
    )
    db.add(export)
    db.commit()

    try:
        source = workdir / "source.glb"
        with storage.open(mesh_asset.storage_key) as src, source.open("wb") as copy:
            shutil.copyfileobj(src, copy)
        out = workdir / f"export.{info.extension}"
        file_notes, validation = _write_checked(
            source,
            fmt,
            out,
            lambda scene: (
                _quad_pairs(db, storage, version.id, scene) if fmt in QUAD_FORMATS else None
            ),
        )
        if file_notes:
            export.options = {
                **(export.options or {}),
                "notes": [*_notes(fmt, version), *file_notes],
            }
        if (version.stats or {}).get("scale") and validation["units"].startswith("model units"):
            validation["units"] = "metres"  # the user gave the model a real-world scale
        key = model_asset_key(
            model.project_id,
            model.id,
            version.version_number,
            f"exports/{export.id}.{info.extension}",
        )
        storage.save_file(key, out)
        asset = Asset(
            project_id=model.project_id,
            model_version_id=version.id,
            kind=AssetKind.EXPORT_FILE,
            storage_key=key,
            mime_type=info.media_type,
            size_bytes=out.stat().st_size,
            sha256=hashlib.sha256(out.read_bytes()).hexdigest(),
            meta={"format": fmt.value},
        )
        db.add(asset)
        db.flush()
        export.asset_id = asset.id
        export.status = ExportStatus.READY
        export.validation = validation
    except exporters.ExportError as exc:
        export.status = ExportStatus.FAILED
        export.error_message = str(exc)
    except Exception:
        log.exception("export failed", extra={"version_id": str(version_id), "format": fmt.value})
        export.status = ExportStatus.FAILED
        export.error_message = "The export failed unexpectedly. See the backend log."
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    export.completed_at = utcnow()
    db.commit()
    return export


def _this_worker() -> dict[str, Any]:
    process = psutil.Process()
    return {"host": socket.gethostname(), "pid": process.pid, "started": process.create_time()}


def _worker_alive(worker: dict[str, Any]) -> bool:
    """Whether the process that recorded ``worker`` is still running (same host only)."""
    try:
        process = psutil.Process(int(worker["pid"]))
        return abs(process.create_time() - float(worker["started"])) < 1.0
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False


# Exports without a recorded worker (made before it was recorded), or written on
# another computer sharing the database, count as interrupted after this long.
INTERRUPTED_AFTER = timedelta(hours=6)
INTERRUPTED_MESSAGE = (
    "The export was interrupted: the app stopped while writing it. Nothing was kept; export again."
)


def recover_interrupted(db: Session, storage: StorageBackend, settings: Settings) -> int:
    """Exports left "running" by a backend that stopped part-way (a crash, a forced quit,
    the computer turning off) are marked failed with the reason, and what they left is
    removed: the temporary folder, and the file if it was stored but not recorded. The
    model version is never touched. Returns how many were found."""
    host = socket.gethostname()
    now = utcnow()
    found = 0
    for export in db.scalars(select(Export).where(Export.status == ExportStatus.RUNNING)):
        options = dict(export.options or {})
        worker = options.get("worker") or {}
        if worker.get("host") == host:
            if _worker_alive(worker):
                continue  # another backend process on this computer is still writing it
        elif now - _aware(export.created_at) < INTERRUPTED_AFTER:
            continue
        found += 1
        workdir = options.get("workdir")
        tmp_root = (settings.data_dir / "tmp").resolve()
        if workdir and Path(workdir).resolve().parent == tmp_root:
            shutil.rmtree(workdir, ignore_errors=True)
        version = db.get(ModelVersion, export.model_version_id)
        if version is not None:
            info = exporters.format_info(export.format)
            key = model_asset_key(
                export.project_id,
                version.model_id,
                version.version_number,
                f"exports/{export.id}.{info.extension}",
            )
            if storage.exists(key):
                storage.delete(key)
        export.status = ExportStatus.FAILED
        export.error_message = INTERRUPTED_MESSAGE
        export.completed_at = now
    if found:
        db.commit()
        log.warning("interrupted exports marked failed", extra={"count": found})
    return found


def _aware(moment: datetime) -> datetime:
    """SQLite gives naive datetimes (stored as UTC)."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def list_exports(db: Session, version_id: uuid.UUID) -> list[Export]:
    get_version(db, version_id)
    return list(
        db.scalars(
            select(Export)
            .where(Export.model_version_id == version_id)
            .order_by(Export.created_at.desc())
        )
    )


def get_export(db: Session, export_id: uuid.UUID) -> Export:
    export = db.get(Export, export_id)
    if export is None:
        raise NotFoundError("Export not found.", details={"export_id": str(export_id)})
    return export


def export_file(db: Session, export_id: uuid.UUID) -> tuple[Export, Asset, str]:
    export = get_export(db, export_id)
    asset = db.get(Asset, export.asset_id) if export.asset_id else None
    if export.status != ExportStatus.READY or asset is None:
        raise NotFoundError("This export has no file.")
    version = get_version(db, export.model_version_id)
    model = get_model(db, version.model_id)
    info = exporters.format_info(export.format)
    name = download_filename(model, version, info.extension)
    if info.extension == "zip":
        name = name.replace(".zip", f"-{export.format.value}.zip")
    return export, asset, name


def delete_export(db: Session, storage: StorageBackend, export_id: uuid.UUID) -> None:
    export = get_export(db, export_id)
    asset = db.get(Asset, export.asset_id) if export.asset_id else None
    if asset is not None:
        storage.delete(asset.storage_key)
        db.delete(asset)
    db.delete(export)
    db.commit()


def to_read(db: Session, export: Export) -> ExportRead:
    asset = db.get(Asset, export.asset_id) if export.asset_id else None
    return ExportRead(
        id=export.id,
        model_version_id=export.model_version_id,
        format=export.format,
        status=export.status,
        validation=export.validation,
        notes=list((export.options or {}).get("notes", [])),
        error_message=export.error_message,
        size_bytes=asset.size_bytes if asset else None,
        created_at=export.created_at,
        completed_at=export.completed_at,
    )
