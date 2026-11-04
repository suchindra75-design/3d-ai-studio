"""Storage key layout and key safety rules. Keep every key format in this module."""

import uuid
from pathlib import PurePosixPath


class InvalidStorageKeyError(ValueError):
    pass


def validate_key(key: str) -> PurePosixPath:
    """A canonical relative POSIX key; anything that could alias or escape is refused."""
    pure = PurePosixPath(key)
    if (
        not key
        or "\\" in key
        or "\x00" in key
        or pure.is_absolute()
        or str(pure) != key.rstrip("/")  # non-canonical ("a//b", "a/./b") would alias
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise InvalidStorageKeyError(f"invalid storage key: {key!r}")
    return pure


def project_prefix(project_id: uuid.UUID) -> str:
    return f"projects/{project_id}/"


def image_original_key(project_id: uuid.UUID, image_id: uuid.UUID, extension: str) -> str:
    return f"projects/{project_id}/images/{image_id}/original.{extension}"


def image_thumbnail_key(project_id: uuid.UUID, image_id: uuid.UUID) -> str:
    return f"projects/{project_id}/images/{image_id}/thumbnail.webp"


def image_prefix(project_id: uuid.UUID, image_id: uuid.UUID) -> str:
    return f"projects/{project_id}/images/{image_id}/"


def image_derived_key(project_id: uuid.UUID, image_id: uuid.UUID, extension: str) -> str:
    """A derived image (e.g. the metadata-free copy sent to a provider)."""
    return f"projects/{project_id}/images/{image_id}/derived.{extension}"


def overlap_analysis_key(project_id: uuid.UUID) -> str:
    """Cached photo-overlap measurement (JSON) for the project's current photo set."""
    return f"projects/{project_id}/analysis/overlap.json"


def model_prefix(project_id: uuid.UUID, model_id: uuid.UUID) -> str:
    return f"projects/{project_id}/models/{model_id}/"


def model_asset_key(
    project_id: uuid.UUID, model_id: uuid.UUID, version_number: int, filename: str
) -> str:
    return f"projects/{project_id}/models/{model_id}/v{version_number}/{filename}"
