"""Levels of detail for viewing large versions (D58): built on request, kept as files."""

import hashlib
import logging
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import NotFoundError
from ai3d.models import Asset, ModelVersion
from ai3d.models.enums import AssetKind
from ai3d.processing import lod
from ai3d.processing.mesh import load_model_scene
from ai3d.schemas.lod import LodLevelRead, LodRead
from ai3d.services.models import get_model, get_version, version_file
from ai3d.storage import StorageBackend
from ai3d.storage.keys import model_asset_key

log = logging.getLogger(__name__)

_API = "/api/v1"
# One build per version at a time in this process (others wait, then reuse it).
_locks: dict[uuid.UUID, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(version_id: uuid.UUID) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(version_id, threading.Lock())


def _triangles(version: ModelVersion) -> int:
    return int((version.stats or {}).get("triangle_count") or 0)


def _assets(db: Session, version_id: uuid.UUID) -> list[Asset]:
    assets = db.scalars(
        select(Asset).where(Asset.model_version_id == version_id, Asset.kind == AssetKind.LOD_MESH)
    ).all()
    return sorted(assets, key=lambda a: int(a.meta.get("level", 0)))


def to_read(db: Session, version: ModelVersion) -> LodRead:
    triangles = _triangles(version)
    assets = _assets(db, version.id)
    levels = [
        LodLevelRead(
            level=int(a.meta["level"]),
            triangles=int(a.meta["triangles"]),
            mean_shift=float(a.meta["mean_shift"]),
            max_shift=float(a.meta["max_shift"]),
            size_bytes=a.size_bytes,
            file_url=f"{_API}/model-versions/{version.id}/lod/{a.meta['level']}/file",
        )
        for a in assets
    ]
    unavailable = (version.stats or {}).get("lod_unavailable")
    if levels:
        status = "ready"
    elif not lod.level_targets(triangles):
        status = "not_needed"
    elif unavailable:
        status = "unavailable"
    else:
        status = "missing"
    return LodRead(
        version_id=version.id,
        status=status,
        triangles=triangles,
        levels=levels,
        message=unavailable if status == "unavailable" else None,
    )


def get_lod(db: Session, version_id: uuid.UUID) -> LodRead:
    return to_read(db, get_version(db, version_id))


def build_lod(
    db: Session, storage: StorageBackend, settings: Settings, version_id: uuid.UUID
) -> LodRead:
    """Builds the levels if the version needs them and has none yet (idempotent)."""
    with _lock(version_id):
        version = get_version(db, version_id)
        db.refresh(version)
        current = to_read(db, version)
        if current.status != "missing":
            return current
        version, mesh_asset = version_file(db, version_id, AssetKind.MESH)
        model = get_model(db, version.model_id)
        tmp_root = settings.data_dir / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="lod-", dir=tmp_root))
        saved: list[str] = []
        try:
            source = workdir / "source.glb"
            with storage.open(mesh_asset.storage_key) as src, source.open("wb") as copy:
                shutil.copyfileobj(src, copy)
            levels = lod.build_levels(load_model_scene(source))
            if not levels:
                version.stats = {
                    **(version.stats or {}),
                    "lod_unavailable": "Simplifying this model changed its shape too much, "
                    "so it is always shown in full detail.",
                }
            for level in levels:
                key = model_asset_key(
                    model.project_id,
                    model.id,
                    version.version_number,
                    f"lod/{level.level}.glb",
                )
                path = workdir / f"lod-{level.level}.glb"
                path.write_bytes(level.glb)
                storage.save_file(key, path)
                saved.append(key)
                db.add(
                    Asset(
                        project_id=model.project_id,
                        model_version_id=version.id,
                        kind=AssetKind.LOD_MESH,
                        storage_key=key,
                        mime_type="model/gltf-binary",
                        size_bytes=len(level.glb),
                        sha256=hashlib.sha256(level.glb).hexdigest(),
                        meta={
                            "level": level.level,
                            "triangles": level.triangles,
                            "mean_shift": level.mean_shift,
                            "max_shift": level.max_shift,
                            "seconds": round(level.seconds, 2),
                        },
                    )
                )
                log.info(
                    "LOD level %d: %d triangles in %.1f s",
                    level.level,
                    level.triangles,
                    level.seconds,
                    extra={"version_id": str(version.id)},
                )
            db.commit()
        except IntegrityError:
            # Another process built them meanwhile: keep theirs.
            db.rollback()
        except BaseException:
            db.rollback()
            for key in saved:
                storage.delete(key)
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return to_read(db, get_version(db, version_id))


def lod_file(db: Session, version_id: uuid.UUID, level: int) -> Asset:
    get_version(db, version_id)
    for asset in _assets(db, version_id):
        if int(asset.meta.get("level", 0)) == level:
            return asset
    raise NotFoundError("This version has no such level of detail.")
