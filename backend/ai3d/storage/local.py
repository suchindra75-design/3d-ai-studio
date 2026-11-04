import hashlib
import io
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

from ai3d.storage.base import StoredObject
from ai3d.storage.keys import InvalidStorageKeyError, validate_key

__all__ = ["InvalidStorageKeyError", "LocalFileStorage"]

_CHUNK = 1024 * 1024


class LocalFileStorage:
    """Stores objects as files under ``root``. Writes are atomic (temp + rename)."""

    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise InvalidStorageKeyError(f"storage key escapes root: {key!r}")
        return path

    def _write_atomic(self, key: str, source: BinaryIO) -> StoredObject:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := source.read(_CHUNK):
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return StoredObject(key=key, size_bytes=size, sha256=digest.hexdigest())

    def save_file(self, key: str, source: Path) -> StoredObject:
        with source.open("rb") as fh:
            return self._write_atomic(key, fh)

    def save_bytes(self, key: str, data: bytes) -> StoredObject:
        return self._write_atomic(key, io.BytesIO(data))

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix.rstrip("/"))
        if path == self.root:
            raise InvalidStorageKeyError("refusing to delete the storage root")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self._path(prefix.rstrip("/")) if prefix.strip("/") else self.root
        if base.is_file():
            return [prefix.rstrip("/")]
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in base.rglob("*")
            if path.is_file() and not path.name.startswith(".tmp-")
        )

    def local_path(self, key: str) -> Path | None:
        return self._path(key)

    def healthy(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.W_OK)

    def usage_bytes(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
        return total
