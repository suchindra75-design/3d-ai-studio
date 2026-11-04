from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import ai3d
from ai3d.api.deps import SettingsDep, StorageDep
from ai3d.core.timeutil import utcnow
from ai3d.models.enums import ProcessingMode, QualityMode
from ai3d.processing.images import SUPPORTED_MIME_TYPES
from ai3d.schemas.system import (
    HealthResponse,
    ProcessingModeInfo,
    QualityModeInfo,
    SystemInfo,
    UploadLimits,
)

router = APIRouter(tags=["system"])

QUALITY_MODES = [
    QualityModeInfo(
        id=QualityMode.FAST,
        label="Fast",
        description="Quick previews: provider's fastest settings, standard textures, "
        "basic mesh validation.",
    ),
    QualityModeInfo(
        id=QualityMode.HIGH_QUALITY,
        label="High Quality",
        description="Visual realism: detailed geometry, higher-resolution PBR textures "
        "where supported, advanced mesh cleanup.",
    ),
    QualityModeInfo(
        id=QualityMode.PROFESSIONAL,
        label="Professional",
        description="Production usability: mesh validation, retopology and UV checks where "
        "available, texture baking, detailed quality report.",
    ),
]
PROCESSING_MODES = [
    ProcessingModeInfo(
        id=ProcessingMode.AUTO,
        label="Automatic",
        description="Picks the best available route for the input, quality mode and "
        "hardware. Never uploads images without your permission.",
    ),
    ProcessingModeInfo(
        id=ProcessingMode.LOCAL,
        label="Local",
        description="Runs only on this machine. Images never leave it.",
    ),
    ProcessingModeInfo(
        id=ProcessingMode.CLOUD,
        label="Cloud",
        description="Uses configured external AI providers; images are uploaded to them.",
    ),
]


@router.get("/health", response_model=HealthResponse, summary="Liveness and dependency status")
def health(request: Request, storage: StorageDep) -> JSONResponse:
    db_ok = request.app.state.db.ping()
    storage_ok = storage.healthy()
    body = HealthResponse(
        status="ok" if db_ok and storage_ok else "degraded",
        version=ai3d.__version__,
        database="ok" if db_ok else "unavailable",
        storage="ok" if storage_ok else "unavailable",
        time=utcnow(),
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=200 if db_ok else 503)


@router.get("/system/info", response_model=SystemInfo, summary="Installation facts and limits")
def system_info(settings: SettingsDep, storage: StorageDep) -> SystemInfo:
    return SystemInfo(
        version=ai3d.__version__,
        environment=settings.environment,
        database_backend=settings.database_backend,
        storage_backend=storage.name,
        data_dir=str(settings.data_dir) if settings.expose_paths else None,
        storage_usage_bytes=storage.usage_bytes(),
        limits=UploadLimits(
            max_upload_bytes=settings.max_upload_bytes,
            max_images_per_project=settings.max_images_per_project,
            max_image_pixels=settings.max_image_pixels,
            accepted_mime_types=list(SUPPORTED_MIME_TYPES),
        ),
        quality_modes=QUALITY_MODES,
        processing_modes=PROCESSING_MODES,
    )
