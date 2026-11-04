from collections.abc import Iterator
from pathlib import Path

import pytest

from ai3d.storage import StorageBackend, create_storage
from ai3d.storage.local import InvalidStorageKeyError, LocalFileStorage
from tests.conftest import TEST_S3_ENDPOINT, make_settings

BACKENDS = [
    "local",
    pytest.param(
        "s3",
        marks=pytest.mark.skipif(
            not TEST_S3_ENDPOINT, reason="set AI3D_TEST_S3_ENDPOINT to test S3-compatible storage"
        ),
    ),
]


@pytest.fixture(params=BACKENDS)
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[StorageBackend]:
    """Every backend must pass the same contract tests."""
    overrides = {"storage_backend": "local"} if request.param == "local" else {}
    if request.param == "s3":
        from tests.conftest import s3_test_settings  # noqa: PLC0415

        overrides = s3_test_settings()
    backend = create_storage(make_settings(tmp_path, None, **overrides))
    yield backend
    if request.param == "s3":
        for key in ("a", "projects", "x"):
            backend.delete_prefix(key)


def test_backend_contract(storage: StorageBackend, tmp_path: Path) -> None:
    assert storage.healthy()
    stored = storage.save_bytes("projects/p/a.bin", b"hello")
    assert (stored.size_bytes, stored.key) == (5, "projects/p/a.bin")
    assert stored.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    source = tmp_path / "upload.bin"
    source.write_bytes(b"x" * 3_000_000)
    assert storage.save_file("projects/p/big.bin", source).size_bytes == 3_000_000
    with storage.open("projects/p/big.bin") as fh:
        assert fh.read() == b"x" * 3_000_000
    storage.save_bytes("projects/p/a.bin", b"replaced")  # overwrite
    with storage.open("projects/p/a.bin") as fh:
        assert fh.read() == b"replaced"
    assert storage.usage_bytes() == 3_000_008  # before projects/pq is added
    assert storage.exists("projects/p/a.bin")
    assert not storage.exists("projects/p/missing.bin")
    storage.save_bytes("projects/pq/keep.bin", b"1")
    assert storage.list_keys("projects/p/") == ["projects/p/a.bin", "projects/p/big.bin"]
    assert storage.list_keys("projects/p/a.bin") == ["projects/p/a.bin"]
    assert len(storage.list_keys()) == 3

    # A prefix is a folder: "projects/p" never takes "projects/pq" with it.
    storage.delete_prefix("projects/p/")
    assert not storage.exists("projects/p/a.bin")
    assert not storage.exists("projects/p/big.bin")
    assert storage.exists("projects/pq/keep.bin")
    storage.delete("projects/pq/keep.bin")
    storage.delete("projects/pq/keep.bin")  # missing objects are ignored
    assert not storage.exists("projects/pq/keep.bin")


@pytest.mark.parametrize("key", ["", "/etc/passwd", "../outside", "a//b", "a\\b"])
def test_every_backend_rejects_unsafe_keys(storage: StorageBackend, key: str) -> None:
    with pytest.raises(InvalidStorageKeyError):
        storage.save_bytes(key, b"x")
    with pytest.raises(InvalidStorageKeyError):
        storage.delete_prefix("/")


@pytest.mark.parametrize(
    "key",
    ["", "/etc/passwd", "../outside", "a/../../outside", "a/./b", "a//b", "a\\b", "nul\x00l"],
)
def test_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    storage = LocalFileStorage(tmp_path / "root")
    with pytest.raises(InvalidStorageKeyError):
        storage.save_bytes(key, b"x")


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    storage = LocalFileStorage(root)
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(InvalidStorageKeyError):
        storage.save_bytes("link/escaped.txt", b"x")


def test_save_open_delete(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    stored = storage.save_bytes("projects/p/a.bin", b"hello")
    assert stored.size_bytes == 5
    assert stored.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    with storage.open("projects/p/a.bin") as fh:
        assert fh.read() == b"hello"
    assert storage.usage_bytes() == 5
    storage.delete_prefix("projects/p/")
    assert not storage.exists("projects/p/a.bin")
    storage.delete("projects/p/a.bin")  # missing objects are ignored


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    storage.save_bytes("x/y.bin", b"1")
    storage.save_bytes("x/y.bin", b"22")  # overwrite is atomic
    assert [p.name for p in (tmp_path / "x").iterdir()] == ["y.bin"]
    with storage.open("x/y.bin") as fh:
        assert fh.read() == b"22"


def test_refuses_to_delete_root(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(InvalidStorageKeyError):
        storage.delete_prefix("/")


@pytest.mark.skipif(not TEST_S3_ENDPOINT, reason="set AI3D_TEST_S3_ENDPOINT")
def test_s3_with_wrong_credentials_is_unhealthy_and_refuses_writes(tmp_path: Path) -> None:
    from tests.conftest import s3_test_settings  # noqa: PLC0415

    values = {**s3_test_settings(), "s3_secret_access_key": "not-the-secret"}
    storage = create_storage(make_settings(tmp_path, None, **values))
    assert storage.healthy() is False
    with pytest.raises(Exception, match=r"SignatureDoesNotMatch|AccessDenied|InvalidAccessKeyId"):
        storage.save_bytes("projects/p/a.bin", b"x")
