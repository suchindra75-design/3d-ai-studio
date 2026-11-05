import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ai3d.core.config import Settings
from ai3d.main import create_app
from ai3d.providers.mock import MockProvider
from ai3d.workers import JobRunner
from tests import images

# Run the suite against another database (e.g. PostgreSQL) by setting
#   AI3D_TEST_DATABASE_URL=postgresql+psycopg://user:pass@host:port/db
# Its public schema is wiped before every test. Default: a fresh SQLite file.
TEST_DATABASE_URL = os.environ.get("AI3D_TEST_DATABASE_URL")

# Optional: run the whole suite on S3-compatible storage (e.g. MinIO), like
#   AI3D_TEST_S3_ENDPOINT=http://127.0.0.1:9000 AI3D_TEST_S3_BUCKET=ai3d-test
#   AI3D_TEST_S3_ACCESS_KEY_ID=... AI3D_TEST_S3_SECRET_ACCESS_KEY=...
# Every test gets its own prefix in the bucket. Default: the local data folder.
TEST_S3_ENDPOINT = os.environ.get("AI3D_TEST_S3_ENDPOINT")


def s3_test_settings() -> dict[str, Any]:
    """Settings for a fresh, empty prefix in the test bucket (bucket created if needed)."""
    import boto3  # noqa: PLC0415 - optional (s3 extra)

    bucket = os.environ.get("AI3D_TEST_S3_BUCKET", "ai3d-test")
    access_key = os.environ.get("AI3D_TEST_S3_ACCESS_KEY_ID")
    secret_key = os.environ.get("AI3D_TEST_S3_SECRET_ACCESS_KEY")
    client = boto3.client(
        "s3",
        endpoint_url=TEST_S3_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
    if bucket not in existing:
        client.create_bucket(Bucket=bucket)
    return {
        "storage_backend": "s3",
        "s3_bucket": bucket,
        "s3_prefix": f"test-{uuid.uuid4().hex}",
        "s3_endpoint_url": TEST_S3_ENDPOINT,
        "s3_region": "us-east-1",
        "s3_access_key_id": access_key,
        "s3_secret_access_key": secret_key,
    }


def reset_external_database(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    if TEST_DATABASE_URL:
        reset_external_database(TEST_DATABASE_URL)
        return TEST_DATABASE_URL
    return f"sqlite:///{tmp_path / 'test.sqlite3'}"


def make_settings(tmp_path: Path, database_url: str | None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "data_dir": tmp_path / "data",
        "database_url": database_url,
        "log_level": "WARNING",
        "allowed_hosts": ["testserver", "127.0.0.1", "localhost"],
        # Tests drive the job runner explicitly (runner.run_until_idle()).
        "run_jobs": False,
        "provider_poll_interval": 0,
        # The COLMAP integration tests run only when this points at COLMAP 4.
        "colmap_path": os.environ.get("AI3D_COLMAP_PATH") or None,
        # The local AI engine is used only when this points at an installed engine
        # (an engine in the default folder must not change other tests' routing).
        "local_ai_dir": os.environ.get("AI3D_LOCAL_AI_DIR") or tmp_path / "no-local-ai",
        # Likewise OpenMVS (dense photogrammetry): only when this points at its programs.
        "openmvs_dir": os.environ.get("AI3D_OPENMVS_DIR") or tmp_path / "no-openmvs",
        **(s3_test_settings() if TEST_S3_ENDPOINT else {}),
        **overrides,
    }
    return Settings(_env_file=None, **values)  # never read a developer's .env in tests


@pytest.fixture
def settings(tmp_path: Path, database_url: str) -> Settings:
    return make_settings(tmp_path, database_url)


@pytest.fixture(autouse=True)
def _reset_mock_provider() -> Iterator[None]:
    MockProvider.reset()
    yield
    MockProvider.reset()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def no_local_ai(app: FastAPI, tmp_path: Path) -> None:
    """No local AI engine, even when AI3D_LOCAL_AI_DIR points at one (for tests of
    what happens without it). The app reads the folder when it is created."""
    from ai3d_engine.local_ai import engine as local_ai_engine  # noqa: PLC0415

    local_ai_engine.configure(tmp_path / "no-local-ai")


@pytest.fixture
def runner(app: FastAPI) -> JobRunner:
    runner: JobRunner = app.state.runner
    return runner


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:  # runs lifespan -> migrations
        yield test_client


def stored_keys(client: TestClient, prefix: str = "") -> list[str]:
    """Keys in the app's storage backend (data folder or S3 bucket)."""
    keys: list[str] = client.app.state.storage.list_keys(prefix)  # type: ignore[attr-defined]
    return keys


@pytest.fixture
def make_project(client: TestClient) -> Callable[..., dict[str, Any]]:
    def _make(**overrides: Any) -> dict[str, Any]:
        body = {"name": "Test object", "input_mode": "multi_view", **overrides}
        response = client.post("/api/v1/projects", json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())

    return _make


@pytest.fixture
def upload(client: TestClient) -> Callable[..., Any]:
    def _upload(project_id: str, *files: tuple[str, bytes, str]) -> Any:
        response = client.post(
            f"/api/v1/projects/{project_id}/images",
            files=[("files", f) for f in files],
        )
        assert response.status_code == 200, response.text
        return response.json()

    return _upload


@pytest.fixture
def sharp_jpeg() -> bytes:
    return images.encode(images.textured(), "JPEG", quality=92)


@pytest.fixture
def make_image_project(
    client: TestClient, make_project: Callable[..., dict[str, Any]], upload: Callable[..., Any]
) -> Callable[..., tuple[dict[str, Any], list[str]]]:
    """A project with ``count`` distinct textured photos. Returns (project, image ids)."""

    def _make(count: int = 1, **overrides: Any) -> tuple[dict[str, Any], list[str]]:
        project = make_project(**overrides)
        files = [
            (f"photo{i}.jpg", images.encode(images.textured(seed=i), "JPEG"), "image/jpeg")
            for i in range(count)
        ]
        result = upload(project["id"], *files)
        ids = [r["image"]["id"] for r in result["results"]]
        assert len(ids) == count
        return project, ids

    return _make
