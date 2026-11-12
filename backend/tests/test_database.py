"""Migrations, constraints and restart persistence."""

import uuid
from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

import ai3d.models  # noqa: F401
from ai3d.core.config import Settings
from ai3d.core.timeutil import utcnow
from ai3d.database.base import Base
from ai3d.database.migrate import downgrade_to_base, upgrade_to_head
from ai3d.database.session import Database
from ai3d.main import create_app
from ai3d.models import GenerationJob, Model3D, ModelVersion, Project
from ai3d.models.enums import (
    InputMode,
    JobKind,
    ModelVersionSource,
    ProcessingMode,
    QualityMode,
)
from tests import images

EXPECTED_TABLES = {
    "projects",
    "images",
    "generation_jobs",
    "job_inputs",
    "job_events",
    "ai_providers",
    "provider_configurations",
    "models",
    "model_versions",
    "model_parts",
    "assets",
    "quality_reports",
    "exports",
    "local_devices",
    "app_settings",
    "scenes",
}


@pytest.fixture
def database(database_url: str) -> Iterator[Database]:
    db = Database(database_url)
    upgrade_to_head(db.engine)
    yield db
    db.dispose()


def test_migrations_create_all_tables(database: Database) -> None:
    tables = set(inspect(database.engine).get_table_names())
    assert tables - {"alembic_version"} == EXPECTED_TABLES


def test_migrations_match_models(database: Database) -> None:
    with database.engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    assert diff == []


def test_downgrade_and_reupgrade(database: Database) -> None:
    downgrade_to_base(database.engine)
    assert set(inspect(database.engine).get_table_names()) <= {"alembic_version"}
    upgrade_to_head(database.engine)
    assert set(inspect(database.engine).get_table_names()) >= EXPECTED_TABLES


def test_sqlite_pragmas(database: Database) -> None:
    if database.engine.dialect.name != "sqlite":
        pytest.skip("SQLite-specific")
    with database.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_foreign_keys_and_cascades(database: Database) -> None:
    with database.session() as session:
        project = Project(name="p", input_mode=InputMode.SINGLE_IMAGE)
        session.add(project)
        session.flush()
        model = Model3D(project_id=project.id, name="m")
        session.add(model)
        session.flush()
        version = ModelVersion(
            model_id=model.id, version_number=1, source=ModelVersionSource.GENERATED
        )
        session.add(version)
        session.flush()
        model.current_version_id = version.id  # cyclic FK works
        session.add(
            GenerationJob(
                project_id=project.id,
                kind=JobKind.IMAGE_TO_3D,
                quality_mode=QualityMode.FAST,
                processing_mode=ProcessingMode.AUTO,
                queued_at=utcnow(),
            )
        )
        project_id = project.id

    with database.session() as session:
        session.delete(session.get(Project, project_id))

    with database.engine.connect() as conn:
        for table in ("models", "model_versions", "generation_jobs"):
            assert conn.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar() == 0

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(Model3D(project_id=uuid.uuid4(), name="orphan"))


def test_cyclic_foreign_keys_exist(database: Database) -> None:
    inspector = inspect(database.engine)
    fks = {
        (table, tuple(fk["constrained_columns"]), fk["referred_table"])
        for table in ("models", "model_versions")
        for fk in inspector.get_foreign_keys(table)
    }
    assert ("models", ("current_version_id",), "model_versions") in fks
    assert ("model_versions", ("primary_asset_id",), "assets") in fks
    assert ("model_versions", ("job_id",), "generation_jobs") in fks


def test_timestamps_are_timezone_aware(database: Database) -> None:
    with database.session() as session:
        session.add(Project(name="p", input_mode=InputMode.MULTI_VIEW))
    with database.session() as session:
        project = session.query(Project).one()
        assert project.created_at.tzinfo is not None
        assert project.created_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_data_survives_restart(settings: Settings) -> None:
    """Create data, shut the app down, start a new instance on the same data dir."""
    jpeg = images.encode(images.textured(), "JPEG")
    with TestClient(create_app(settings)) as client:
        pid = client.post(
            "/api/v1/projects", json={"name": "Persisted", "input_mode": "single_image"}
        ).json()["id"]
        image = client.post(
            f"/api/v1/projects/{pid}/images", files=[("files", ("a.jpg", jpeg, "image/jpeg"))]
        ).json()
        image_id = image["results"][0]["image"]["id"]

    with TestClient(create_app(settings)) as client:
        project = client.get(f"/api/v1/projects/{pid}").json()
        assert project["name"] == "Persisted"
        assert project["image_count"] == 1
        assert client.get(f"/api/v1/images/{image_id}/file").content == jpeg
