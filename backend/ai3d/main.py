"""Application factory.

Run with ``ai3d-server`` (see ``ai3d.cli``) or
``uvicorn --factory ai3d.main:create_app``.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from httpx2 import BaseTransport

import ai3d
from ai3d.api.errors import install_error_handlers
from ai3d.api.middleware import (
    ApiTokenMiddleware,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
)
from ai3d.api.v1 import API_PREFIX
from ai3d.api.v1 import router as v1_router
from ai3d.core.config import Settings
from ai3d.core.error_reporting import setup_error_reporting
from ai3d.core.logging import configure_logging
from ai3d.core.secrets import SecretBox
from ai3d.database.migrate import upgrade_to_head
from ai3d.database.session import Database
from ai3d.providers import ProviderRegistry
from ai3d.services.providers import ProviderManager
from ai3d.storage import create_storage
from ai3d.workers import JobRunner
from ai3d_engine import local_ai
from ai3d_engine.runtime import configure_tool

log = logging.getLogger(__name__)

# Generous ceiling for one multipart request; per-file limits are enforced separately.
_MAX_FILES_PER_REQUEST = 50


def create_app(
    settings: Settings | None = None, *, provider_transport: BaseTransport | None = None
) -> FastAPI:
    """Build the application.

    ``provider_transport`` replaces the network for provider HTTP calls (tests).
    """
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)
    setup_error_reporting(settings)
    settings.ensure_directories()
    configure_tool("colmap", settings.colmap_path)
    local_ai.configure(settings.local_ai_dir)

    database = Database(settings.resolved_database_url)
    storage = create_storage(settings)
    providers = ProviderManager(
        settings,
        ProviderRegistry(settings),
        SecretBox.from_settings(settings),
        transport=provider_transport,
    )
    runner = JobRunner(database=database, storage=storage, settings=settings, providers=providers)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if settings.auto_migrate:
            upgrade_to_head(database.engine)
        with database.session() as db:
            providers.sync(db)
        if settings.run_jobs:
            runner.start()
        log.info(
            "AI 3D Studio backend started",
            extra={
                "version": ai3d.__version__,
                "database": settings.database_backend,
                "data_dir": str(settings.data_dir),
            },
        )
        yield
        runner.stop()
        database.dispose()

    app = FastAPI(
        title="AI 3D Studio API",
        version=ai3d.__version__,
        summary="Local-first image-to-3D reconstruction platform.",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = database
    app.state.storage = storage
    app.state.providers = providers
    app.state.runner = runner

    install_error_handlers(app)
    if settings.api_token is not None:
        # Inside CORS, so 401s carry CORS headers and preflights still pass.
        app.add_middleware(ApiTokenMiddleware, token=settings.api_token.get_secret_value())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "X-Request-ID",
            "Last-Event-ID",
        ],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_upload_bytes * _MAX_FILES_PER_REQUEST + 1024 * 1024,
    )
    if "*" not in settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(v1_router)
    return app
