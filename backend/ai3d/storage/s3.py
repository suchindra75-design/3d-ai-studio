"""S3-compatible object storage (AWS S3, MinIO, Ceph, Cloudflare R2, ...).

Optional: needs the ``s3`` extra and ``AI3D_STORAGE_BACKEND=s3``. Objects live
under ``AI3D_S3_PREFIX`` in one bucket. Each upload is a single PUT, which S3
makes visible atomically. Processing that needs a file on disk copies the object
to a temporary folder first (``local_path`` returns None), and downloads are
streamed through the backend, so the bucket can stay private.

Credentials come from ``AI3D_S3_ACCESS_KEY_ID``/``AI3D_S3_SECRET_ACCESS_KEY``
when set, otherwise from the standard AWS chain (environment, shared config,
instance role). They are never logged or sent to the browser.
"""

import hashlib
import io
from pathlib import Path
from typing import Any, BinaryIO

from ai3d.storage.base import StoredObject
from ai3d.storage.keys import InvalidStorageKeyError, validate_key

_CHUNK = 1024 * 1024
_DELETE_BATCH = 1000  # S3's DeleteObjects limit


class S3Storage:
    name = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: Any = None,
    ) -> None:
        if client is None:
            import boto3  # noqa: PLC0415 - optional dependency
            from botocore.config import Config  # noqa: PLC0415

            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                config=Config(retries={"mode": "standard", "max_attempts": 5}),
            )
        self.client = client
        self.bucket = bucket
        self.prefix = f"{validate_key(prefix.strip('/'))}/" if prefix.strip("/") else ""

    def _key(self, key: str) -> str:
        return self.prefix + str(validate_key(key))

    def _put(self, key: str, body: BinaryIO, size: int, sha256: str) -> StoredObject:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._key(key),
            Body=body,
            ContentLength=size,
            Metadata={"sha256": sha256},
        )
        return StoredObject(key=key, size_bytes=size, sha256=sha256)

    def save_file(self, key: str, source: Path) -> StoredObject:
        digest = hashlib.sha256()
        with source.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
        with source.open("rb") as fh:
            return self._put(key, fh, source.stat().st_size, digest.hexdigest())

    def save_bytes(self, key: str, data: bytes) -> StoredObject:
        return self._put(key, io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest())

    def open(self, key: str) -> BinaryIO:
        response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        body: BinaryIO = response["Body"]
        return body

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def _keys_under(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return keys

    def delete_prefix(self, prefix: str) -> None:
        stripped = prefix.rstrip("/")
        if not stripped:
            raise InvalidStorageKeyError("refusing to delete the storage root")
        # The object itself, or everything below it as a "folder"; never "a/bc" for "a/b".
        keys = [self._key(key) for key in self.list_keys(stripped)]
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )

    def list_keys(self, prefix: str = "") -> list[str]:
        stripped = prefix.rstrip("/")
        full = self._key(stripped) if stripped else self.prefix
        keys = self._keys_under(full)
        if stripped:
            keys = [k for k in keys if k == full or k.startswith(full + "/")]
        return sorted(k[len(self.prefix) :] for k in keys)

    def local_path(self, key: str) -> Path | None:
        validate_key(key)
        return None

    def usage_bytes(self) -> int:
        total = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            total += sum(int(item["Size"]) for item in page.get("Contents", []))
        return total

    def healthy(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:  # any failure means "not usable right now"
            return False
        return True
