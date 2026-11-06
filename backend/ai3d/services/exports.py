"""Exports of a model version: convert, re-open to validate, store, list."""

import hashlib
import io
import logging
import shutil
import tempfile
import uuid
from pathlib import Path

import numpy as np
import trimesh
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import InvalidInputError, NotFoundError
from ai3d.core.timeutil import utcnow
from ai3d.models import Asset, Export, ModelVersion
from ai3d.models.enums import AssetKind, ExportFormat, ExportStatus
from ai3d.processing import exporters, quads
from ai3d.processing.mesh import load_model_scene
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
    export = Export(
        project_id=model.project_id,
        model_version_id=version.id,
        format=fmt,
        status=ExportStatus.RUNNING,
        options={"notes": _notes(fmt, version)},
    )
    db.add(export)
    db.commit()

    tmp_root = settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="export-", dir=tmp_root))
    try:
        source = workdir / "source.glb"
        with storage.open(mesh_asset.storage_key) as src, source.open("wb") as copy:
            shutil.copyfileobj(src, copy)
        scene = load_model_scene(source)
        facts = exporters.scene_facts(scene)
        out = workdir / f"export.{info.extension}"
        pairs = _quad_pairs(db, storage, version.id, scene) if fmt in QUAD_FORMATS else None
        expected = sum(len(p) for p in (pairs or {}).values())
        file_notes = exporters.write(scene, fmt, out, quad_pairs=pairs) or []
        validation = exporters.validate(out, fmt, facts, quads_expected=expected)
        if expected and validation["quads"] < expected:
            file_notes.append(
                f"{validation['quads']:,} of the {expected:,} quads from retopology were "
                "kept; the rest are written as triangles."
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
