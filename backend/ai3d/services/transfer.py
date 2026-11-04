"""Bringing models in and projects out: import a 3D file, back up and restore a project.

An imported model is stored like any other version: converted to GLB, re-opened
and checked; a file that fails the checks is rejected. Its origin is unknown to
the app, which the version says.

A backup is one ZIP: ``manifest.json`` (project settings, model and version
records with notes, stats and quality reports) plus the original photos and
every version's GLB. Restoring creates a *new* project (new IDs), re-imports the
photos through the normal upload checks and re-checks every model file.
"""

import json
import logging
import shutil
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import InvalidInputError
from ai3d.models import Image, Model3D, ModelVersion, QualityReport
from ai3d.models.enums import (
    AssetKind,
    ImageKind,
    InputMode,
    ModelVersionSource,
    ProcessingMode,
    QualityMode,
    ReportStatus,
    SurfaceProvenance,
)
from ai3d.processing import importing
from ai3d.processing.mesh import validate_mesh_file
from ai3d.schemas.project import ProjectCreate, ProjectRead
from ai3d.services import images as image_service
from ai3d.services.models import _spool_model, store_checked_version, version_file
from ai3d.services.projects import create_project, delete_project, get_project, read_project
from ai3d.storage import StorageBackend

log = logging.getLogger(__name__)

BACKUP_FORMAT = "ai3d-studio-project"
BACKUP_VERSION = 1
MAX_MANIFEST_BYTES = 20 * 1024 * 1024
# Quality-report entries that record how a version was made; kept on restore.
_HISTORY_METRICS = ("enhancement", "reconstruction")


def _workdir(settings: Settings, prefix: str) -> Path:
    root = settings.data_dir / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def _check(path: Path, what: str) -> Any:
    report = validate_mesh_file(path, file_type="glb")
    if report.status == ReportStatus.FAIL:
        raise InvalidInputError(
            f"{what} failed the file checks and was not imported.",
            details={"issues": [issue.to_dict() for issue in report.issues]},
        )
    return report


# -- import -------------------------------------------------------------------------


def import_model(
    db: Session,
    storage: StorageBackend,
    settings: Settings,
    project_id: uuid.UUID,
    *,
    filename: str,
    stream: BinaryIO,
    units: importing.Units | None = None,
    up: importing.UpAxis | None = None,
    real_scale: bool = False,
    name: str | None = None,
) -> Model3D:
    """Store an existing 3D file as a new model (version 1, source ``imported``)."""
    project = get_project(db, project_id)
    safe_name = image_service.sanitize_filename(filename)
    workdir = _workdir(settings, "import-")
    try:
        upload = workdir / f"upload{Path(safe_name).suffix.lower()}"
        _spool_model(stream, upload, settings.max_model_bytes)
        converted = importing.convert(upload, safe_name, workdir, units=units, up=up)
        report = _check(converted.glb, "The model")
        stats: dict[str, Any] = {}
        size_note = (
            " Its size is not treated as a real-world size; Enhance can set one from a measurement."
        )
        if real_scale:
            longest = max(report.metrics.get("bbox_size") or [0.0])
            stats["scale"] = {
                "known": True,
                "axis": "longest",
                "length_mm": round(longest * 1000.0, 3),
                "source": "file units",
            }
            size_note = " Its size is taken as real, as you said the file uses real units."
        model = Model3D(project_id=project.id, name=(name or Path(safe_name).stem)[:200] or "Model")
        db.add(model)
        db.flush()
        store_checked_version(
            db,
            storage,
            model=model,
            path=converted.glb,
            size=converted.glb.stat().st_size,
            report=report,
            parent_id=None,
            source=ModelVersionSource.IMPORTED,
            quality_mode=None,
            provider_key=None,
            provenance=SurfaceProvenance.UNKNOWN,
            stats_extra=stats,
            notes=(
                f"Imported from {safe_name} ({converted.format}; {converted.units}; "
                f"{converted.up} up). The app did not make or measure this model, so its "
                f"origin and accuracy are unknown.{size_note}"
            ),
            asset_meta={
                "format": "glb",
                "origin": "import",
                "source_name": safe_name,
                "source_format": converted.format,
            },
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    db.refresh(model)
    return model


# -- backup -------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def backup_project(
    db: Session, storage: StorageBackend, settings: Settings, project_id: uuid.UUID
) -> Path:
    """Write the project's backup ZIP to a temporary file and return its path.

    The caller deletes the file (and its folder) after sending it.
    """
    project = get_project(db, project_id)
    workdir = _workdir(settings, "backup-")
    archive = workdir / "backup.zip"
    images = db.scalars(
        select(Image)
        .where(Image.project_id == project.id, Image.kind == ImageKind.ORIGINAL)
        .order_by(Image.position)
    ).all()
    models = db.scalars(
        select(Model3D).where(Model3D.project_id == project.id).order_by(Model3D.created_at)
    ).all()
    manifest: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "project": {
            "name": project.name,
            "description": project.description,
            "input_mode": project.input_mode.value,
            "default_quality_mode": project.default_quality_mode.value,
            "default_processing_mode": project.default_processing_mode.value,
            "allow_cloud_upload": project.allow_cloud_upload,
            "created_at": _iso(project.created_at),
        },
        "images": [],
        "models": [],
    }
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as bundle:
            for n, image in enumerate(images, start=1):
                suffix = Path(image.original_filename).suffix.lower() or ".jpg"
                member = f"images/{n:04d}{suffix}"
                with storage.open(image.storage_key) as src, bundle.open(member, "w") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                manifest["images"].append(
                    {"file": member, "original_filename": image.original_filename}
                )
            for m, model in enumerate(models, start=1):
                versions = db.scalars(
                    select(ModelVersion)
                    .where(ModelVersion.model_id == model.id)
                    .order_by(ModelVersion.version_number)
                ).all()
                numbers = {v.id: v.version_number for v in versions}
                entry: dict[str, Any] = {
                    "name": model.name,
                    "created_at": _iso(model.created_at),
                    "current_version": numbers.get(model.current_version_id)
                    if model.current_version_id
                    else None,
                    "versions": [],
                }
                for version in versions:
                    _, asset = version_file(db, version.id, AssetKind.MESH)
                    member = f"models/{m:03d}/v{version.version_number}.glb"
                    with storage.open(asset.storage_key) as src, bundle.open(member, "w") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    report = db.scalar(
                        select(QualityReport).where(QualityReport.model_version_id == version.id)
                    )
                    entry["versions"].append(
                        {
                            "number": version.version_number,
                            "parent": numbers.get(version.parent_version_id)
                            if version.parent_version_id
                            else None,
                            "source": version.source.value,
                            "quality_mode": version.quality_mode.value
                            if version.quality_mode
                            else None,
                            "provider_key": version.provider_key,
                            "surface_provenance": version.surface_provenance.value,
                            "stats": version.stats,
                            "notes": version.notes,
                            "created_at": _iso(version.created_at),
                            "file": member,
                            "quality_report": {
                                "status": report.status.value,
                                "metrics": report.metrics,
                                "issues": report.issues,
                            }
                            if report
                            else None,
                        }
                    )
                manifest["models"].append(entry)
            bundle.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, default=str),
                compress_type=zipfile.ZIP_DEFLATED,
            )
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return archive


# -- restore ------------------------------------------------------------------------


def _read_manifest(bundle: zipfile.ZipFile) -> dict[str, Any]:
    try:
        info = bundle.getinfo("manifest.json")
    except KeyError:
        raise InvalidInputError("This is not an AI 3D Studio backup (no manifest.json).") from None
    if info.file_size > MAX_MANIFEST_BYTES:
        raise InvalidInputError("The backup's manifest is too large.")
    try:
        manifest = json.loads(bundle.read(info))
    except ValueError:
        raise InvalidInputError("The backup's manifest is not valid JSON.") from None
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise InvalidInputError("This is not an AI 3D Studio backup.")
    if manifest.get("version") != BACKUP_VERSION:
        raise InvalidInputError(
            f"This backup has format version {manifest.get('version')}; this app reads "
            f"version {BACKUP_VERSION}."
        )
    return manifest


def _member(bundle: zipfile.ZipFile, name: Any, workdir: Path, limit: int) -> Path:
    """Extract one file named by the manifest (never a path of its choosing)."""
    if not isinstance(name, str):
        raise InvalidInputError("The backup's manifest is damaged.")
    try:
        info = bundle.getinfo(name)
    except KeyError:
        raise InvalidInputError(f"The backup is missing {name!r}.") from None
    if info.file_size > limit:
        raise InvalidInputError(f"{name!r} in the backup is too large.")
    target = workdir / f"{uuid.uuid4().hex}{Path(name).suffix.lower()[:10]}"
    with bundle.open(info) as src, target.open("wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)
    return target


def _enum(kind: type[Any], value: Any, default: Any) -> Any:
    try:
        return kind(value)
    except ValueError:
        return default


def restore_project(
    db: Session, storage: StorageBackend, settings: Settings, stream: BinaryIO
) -> ProjectRead:
    """Create a new project from a backup ZIP; on any problem nothing is kept."""
    workdir = _workdir(settings, "restore-")
    created: uuid.UUID | None = None
    try:
        archive = workdir / "backup.zip"
        with archive.open("wb") as out:
            shutil.copyfileobj(stream, out, 1024 * 1024)
        try:
            bundle = zipfile.ZipFile(archive)
        except zipfile.BadZipFile:
            raise InvalidInputError("The backup could not be opened as a ZIP file.") from None
        with bundle:
            manifest = _read_manifest(bundle)
            info = manifest.get("project") or {}
            project = create_project(
                db,
                ProjectCreate(
                    name=f"{str(info.get('name') or 'Project')[:180]} (restored)",
                    description=str(info.get("description") or "")[:2000],
                    input_mode=_enum(InputMode, info.get("input_mode"), InputMode.SINGLE_IMAGE),
                    default_quality_mode=_enum(
                        QualityMode, info.get("default_quality_mode"), QualityMode.HIGH_QUALITY
                    ),
                    default_processing_mode=_enum(
                        ProcessingMode, info.get("default_processing_mode"), ProcessingMode.AUTO
                    ),
                    allow_cloud_upload=bool(info.get("allow_cloud_upload", False)),
                ),
            )
            created = project.id
            incoming = []
            for image in manifest.get("images") or []:
                path = _member(bundle, image.get("file"), workdir, settings.max_upload_bytes)
                incoming.append(
                    image_service.IncomingFile(
                        filename=str(image.get("original_filename") or path.name),
                        stream=path.open("rb"),
                    )
                )
            try:
                if incoming:
                    image_service.upload_images(db, storage, settings, project.id, incoming)
            finally:
                for item in incoming:
                    item.stream.close()
            for entry in manifest.get("models") or []:
                _restore_model(db, storage, settings, bundle, workdir, project.id, entry)
        return read_project(db, project.id)
    except BaseException:
        db.rollback()
        if created is not None:
            delete_project(db, storage, created)
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _restore_model(  # noqa: PLR0917 -- one call site, all needed
    db: Session,
    storage: StorageBackend,
    settings: Settings,
    bundle: zipfile.ZipFile,
    workdir: Path,
    project_id: uuid.UUID,
    entry: dict[str, Any],
) -> None:
    versions = sorted(entry.get("versions") or [], key=lambda v: int(v.get("number", 0)))
    if not versions:
        return
    model = Model3D(project_id=project_id, name=str(entry.get("name") or "Model")[:200])
    db.add(model)
    db.flush()
    ids: dict[int, uuid.UUID] = {}
    for record in versions:
        number = int(record["number"])
        path = _member(bundle, record.get("file"), workdir, settings.max_model_bytes)
        report = _check(path, f"Version {number} of “{model.name}”")
        old_report = record.get("quality_report") or {}
        old_metrics = old_report.get("metrics") or {}
        stats = record.get("stats") or {}
        version = store_checked_version(
            db,
            storage,
            model=model,
            path=path,
            size=path.stat().st_size,
            report=report,
            parent_id=ids.get(int(record["parent"])) if record.get("parent") else None,
            source=_enum(ModelVersionSource, record.get("source"), ModelVersionSource.IMPORTED),
            quality_mode=_enum(QualityMode, record.get("quality_mode"), None)
            if record.get("quality_mode")
            else None,
            provider_key=record.get("provider_key"),
            provenance=_enum(
                SurfaceProvenance, record.get("surface_provenance"), SurfaceProvenance.UNKNOWN
            ),
            stats_extra={"scale": stats["scale"]} if stats.get("scale") else {},
            notes=str(record.get("notes") or ""),
            asset_meta={"format": "glb", "origin": "restore"},
            metrics_extra={k: old_metrics[k] for k in _HISTORY_METRICS if k in old_metrics},
            version_number=number,
        )
        ids[number] = version.id
        path.unlink(missing_ok=True)
    current_number = entry.get("current_version")
    current = ids.get(int(current_number)) if isinstance(current_number, int) else None
    if current is not None:
        model.current_version_id = current
        db.commit()
    log.info("model restored", extra={"model_id": str(model.id), "versions": len(ids)})
