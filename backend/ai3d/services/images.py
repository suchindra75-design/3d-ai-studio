"""Image upload, listing and deletion.

Uploads are processed one file at a time so a single bad file never fails a
batch. Each file is streamed to a temporary file (enforcing the size limit),
validated by content, analysed, and only then copied into storage and
recorded in the database.
"""

import hashlib
import logging
import re
import shutil
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.errors import AppError, LimitExceededError, NotFoundError, PayloadTooLargeError
from ai3d.models import Image
from ai3d.models.enums import ImageKind, QualityLevel
from ai3d.processing import images as imgproc
from ai3d.processing.preprocess import preprocess_for_provider, preprocess_signature
from ai3d.schemas.common import ErrorBody
from ai3d.schemas.image import ImageRead, UploadItemResult, UploadResponse
from ai3d.services.projects import get_project, touch_project
from ai3d.storage import StorageBackend, image_original_key, image_thumbnail_key
from ai3d.storage.keys import image_derived_key, image_prefix

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024


@dataclass(slots=True)
class IncomingFile:
    filename: str
    stream: BinaryIO


def sanitize_filename(name: str | None) -> str:
    """Display-only filename: basename, no control characters, ≤ 255 chars."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = unicodedata.normalize("NFC", base)
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C").strip()
    return base[:255] or "image"


def _spool(stream: BinaryIO, target: Path, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with target.open("wb") as out:
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise PayloadTooLargeError(
                    f"File exceeds the {max_bytes // (1024 * 1024)} MB upload limit.",
                    details={"max_upload_bytes": max_bytes},
                )
            digest.update(chunk)
            out.write(chunk)
    if size == 0:
        raise AppError("File is empty.", code="empty_file", status_code=422)
    return size, digest.hexdigest()


def _find_duplicate(db: Session, project_id: uuid.UUID, sha256: str) -> Image | None:
    return db.scalar(
        select(Image).where(
            Image.project_id == project_id,
            Image.sha256 == sha256,
            Image.kind == ImageKind.ORIGINAL,
        )
    )


def _count_originals(db: Session, project_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count(Image.id)).where(
                Image.project_id == project_id, Image.kind == ImageKind.ORIGINAL
            )
        )
        or 0
    )


def _next_position(db: Session, project_id: uuid.UUID) -> int:
    current = db.scalar(select(func.max(Image.position)).where(Image.project_id == project_id))
    return (current or 0) + 1


def _process_one(
    db: Session,
    *,
    storage: StorageBackend,
    settings: Settings,
    project_id: uuid.UUID,
    incoming: IncomingFile,
    workdir: Path,
) -> UploadItemResult:
    filename = sanitize_filename(incoming.filename)
    tmp_path = workdir / uuid.uuid4().hex
    size, sha256 = _spool(incoming.stream, tmp_path, settings.max_upload_bytes)

    duplicate = _find_duplicate(db, project_id, sha256)
    if duplicate is not None:
        return UploadItemResult(
            filename=filename, status="duplicate", image=ImageRead.model_validate(duplicate)
        )

    if _count_originals(db, project_id) >= settings.max_images_per_project:
        raise LimitExceededError(
            f"A project can hold at most {settings.max_images_per_project} images.",
            details={"max_images_per_project": settings.max_images_per_project},
        )

    with imgproc.open_validated(tmp_path, max_pixels=settings.max_image_pixels) as img:
        meta = imgproc.extract_metadata(img)
        analysis = imgproc.analyze(img, meta)
        with imgproc.oriented(img) as upright:
            thumbnail = imgproc.make_thumbnail(
                upright, size=settings.thumbnail_size, has_alpha=meta.has_alpha
            )

    image_id = uuid.uuid4()
    original_key = image_original_key(project_id, image_id, meta.extension)
    thumb_key = image_thumbnail_key(project_id, image_id)
    image = Image(
        id=image_id,
        project_id=project_id,
        kind=ImageKind.ORIGINAL,
        position=_next_position(db, project_id),
        original_filename=filename,
        storage_key=original_key,
        thumbnail_key=thumb_key,
        mime_type=meta.mime_type,
        size_bytes=size,
        sha256=sha256,
        width=meta.width,
        height=meta.height,
        exif_orientation=meta.exif_orientation,
        camera_make=meta.camera_make,
        camera_model=meta.camera_model,
        focal_length_mm=meta.focal_length_mm,
        focal_length_35mm=meta.focal_length_35mm,
        exif=meta.exif,
        quality_level=QualityLevel(analysis.level),
        sharpness=analysis.sharpness,
        analysis=analysis.to_dict(),
    )
    try:
        storage.save_file(original_key, tmp_path)  # bytes preserved exactly as uploaded
        storage.save_bytes(thumb_key, thumbnail)
        db.add(image)
        touch_project(db, project_id)
        db.commit()
    except IntegrityError:
        # A concurrent upload of the same bytes won the race.
        db.rollback()
        storage.delete_prefix(image_prefix(project_id, image_id))
        existing = _find_duplicate(db, project_id, sha256)
        if existing is None:
            raise
        return UploadItemResult(
            filename=filename, status="duplicate", image=ImageRead.model_validate(existing)
        )
    except BaseException:
        db.rollback()
        storage.delete_prefix(image_prefix(project_id, image_id))
        raise

    log.info(
        "image uploaded",
        extra={
            "project_id": str(project_id),
            "image_id": str(image_id),
            "bytes": size,
            "quality": analysis.level,
        },
    )
    return UploadItemResult(
        filename=filename, status="created", image=ImageRead.model_validate(image)
    )


def upload_images(
    db: Session,
    storage: StorageBackend,
    settings: Settings,
    project_id: uuid.UUID,
    files: list[IncomingFile],
) -> UploadResponse:
    get_project(db, project_id)
    tmp_root = settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="upload-", dir=tmp_root))
    results: list[UploadItemResult] = []
    try:
        for incoming in files:
            try:
                results.append(
                    _process_one(
                        db,
                        storage=storage,
                        settings=settings,
                        project_id=project_id,
                        incoming=incoming,
                        workdir=workdir,
                    )
                )
            except AppError as exc:
                results.append(
                    UploadItemResult(
                        filename=sanitize_filename(incoming.filename),
                        status="rejected",
                        error=ErrorBody(code=exc.code, message=exc.message, details=exc.details),
                    )
                )
            except Exception:
                log.exception("image processing failed", extra={"project_id": str(project_id)})
                results.append(
                    UploadItemResult(
                        filename=sanitize_filename(incoming.filename),
                        status="rejected",
                        error=ErrorBody(
                            code="processing_failed",
                            message="The image could not be processed.",
                        ),
                    )
                )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return UploadResponse(
        results=results,
        created=sum(r.status == "created" for r in results),
        duplicates=sum(r.status == "duplicate" for r in results),
        rejected=sum(r.status == "rejected" for r in results),
    )


def list_images(db: Session, project_id: uuid.UUID) -> list[Image]:
    get_project(db, project_id)
    return list(
        db.scalars(
            select(Image)
            .where(Image.project_id == project_id, Image.kind == ImageKind.ORIGINAL)
            .order_by(Image.position, Image.created_at)
        )
    )


def get_image(db: Session, image_id: uuid.UUID) -> Image:
    image = db.get(Image, image_id)
    if image is None:
        raise NotFoundError("Image not found.", details={"image_id": str(image_id)})
    return image


def delete_image(db: Session, storage: StorageBackend, image_id: uuid.UUID) -> None:
    image = get_image(db, image_id)
    derived = list(db.scalars(select(Image).where(Image.derived_from_id == image_id)))
    prefixes = [image_prefix(image.project_id, i.id) for i in [image, *derived]]
    project_id = image.project_id
    db.delete(image)
    touch_project(db, project_id)
    db.commit()
    for prefix in prefixes:
        try:
            storage.delete_prefix(prefix)
        except OSError:
            log.exception("failed to delete image files", extra={"prefix": prefix})


def _find_provider_copy(db: Session, source: Image, signature: dict[str, int]) -> Image | None:
    for derived in db.scalars(
        select(Image).where(
            Image.derived_from_id == source.id, Image.kind == ImageKind.PREPROCESSED
        )
    ):
        if derived.analysis.get("preprocess") == signature:
            return derived
    return None


def ensure_provider_copy(
    db: Session,
    storage: StorageBackend,
    settings: Settings,
    source: Image,
    *,
    max_bytes: int,
) -> Image:
    """The upright, resized, metadata-free copy of ``source`` that providers receive.

    Copies are cached as ``kind=preprocessed`` images derived from the original
    and deleted together with it.
    """
    signature = preprocess_signature(settings.provider_image_max_side)
    cached = _find_provider_copy(db, source, signature)
    if cached is not None and storage.exists(cached.storage_key):
        return cached

    tmp_root = settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="prep-", dir=tmp_root))
    try:
        original = workdir / "original"
        with storage.open(source.storage_key) as src, original.open("wb") as out:
            shutil.copyfileobj(src, out, _CHUNK)
        prepared = preprocess_for_provider(
            original,
            max_pixels=settings.max_image_pixels,
            max_side=settings.provider_image_max_side,
            max_bytes=max_bytes,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    sha256 = hashlib.sha256(prepared.data).hexdigest()
    derived_id = uuid.uuid4()
    key = image_derived_key(source.project_id, derived_id, prepared.extension)
    stem = source.original_filename.rsplit(".", 1)[0]
    derived = Image(
        id=derived_id,
        project_id=source.project_id,
        kind=ImageKind.PREPROCESSED,
        derived_from_id=source.id,
        position=source.position,
        original_filename=f"{stem} (provider copy).{prepared.extension}"[:255],
        storage_key=key,
        mime_type=prepared.mime_type,
        size_bytes=len(prepared.data),
        sha256=sha256,
        width=prepared.width,
        height=prepared.height,
        exif={},
        quality_level=source.quality_level,
        sharpness=source.sharpness,
        analysis={
            "preprocess": signature,
            "resized": prepared.resized,
            "color_converted": prepared.color_converted,
            "metadata_removed": True,
        },
    )
    try:
        storage.save_bytes(key, prepared.data)
        if cached is not None:  # stale record whose file is missing
            db.delete(cached)
        db.add(derived)
        db.commit()
    except IntegrityError:
        # Another original produced identical pixels (e.g. same photo, different EXIF).
        db.rollback()
        storage.delete_prefix(image_prefix(source.project_id, derived_id))
        existing = db.scalar(
            select(Image).where(
                Image.project_id == source.project_id,
                Image.sha256 == sha256,
                Image.kind == ImageKind.PREPROCESSED,
            )
        )
        if existing is None:
            raise
        return existing
    except BaseException:
        db.rollback()
        storage.delete_prefix(image_prefix(source.project_id, derived_id))
        raise
    return derived
