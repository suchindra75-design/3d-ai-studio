"""Binary storage. The database stores keys; bytes live in a StorageBackend."""

from pydantic import SecretStr

from ai3d.core.config import Settings
from ai3d.storage.base import StorageBackend, StoredObject
from ai3d.storage.keys import image_original_key, image_thumbnail_key, project_prefix
from ai3d.storage.local import LocalFileStorage


def create_storage(settings: Settings) -> StorageBackend:
    """The storage backend the settings ask for."""
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:
            raise ValueError("AI3D_STORAGE_BACKEND=s3 needs AI3D_S3_BUCKET")
        try:
            from ai3d.storage.s3 import S3Storage  # noqa: PLC0415 - optional dependency

            return S3Storage(
                settings.s3_bucket,
                prefix=settings.s3_prefix,
                endpoint_url=settings.s3_endpoint_url,
                region=settings.s3_region,
                access_key_id=secret(settings.s3_access_key_id),
                secret_access_key=secret(settings.s3_secret_access_key),
            )
        except ImportError as exc:
            raise RuntimeError(
                "AI3D_STORAGE_BACKEND=s3 needs the s3 extra: uv sync --extra s3"
            ) from exc
    return LocalFileStorage(settings.storage_dir)


def secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


__all__ = [
    "LocalFileStorage",
    "StorageBackend",
    "StoredObject",
    "create_storage",
    "image_original_key",
    "image_thumbnail_key",
    "project_prefix",
]
