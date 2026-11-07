"""FastAPI dependencies. Application-wide objects live on ``app.state``."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.database.session import Database
from ai3d.services.providers import ProviderManager
from ai3d.storage import StorageBackend
from ai3d.workers import JobRunner


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.db
    return database


def get_storage(request: Request) -> StorageBackend:
    storage: StorageBackend = request.app.state.storage
    return storage


def get_db(database: Annotated[Database, Depends(get_database)]) -> Iterator[Session]:
    session = database.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SettingsDep = Annotated[Settings, Depends(get_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
DbDep = Annotated[Session, Depends(get_db)]
StorageDep = Annotated[StorageBackend, Depends(get_storage)]


def get_provider_manager(request: Request) -> ProviderManager:
    manager: ProviderManager = request.app.state.providers
    return manager


def get_runner(request: Request) -> JobRunner:
    runner: JobRunner = request.app.state.runner
    return runner


ProvidersDep = Annotated[ProviderManager, Depends(get_provider_manager)]
RunnerDep = Annotated[JobRunner, Depends(get_runner)]
