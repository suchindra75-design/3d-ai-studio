from datetime import datetime
from typing import Literal

from ai3d.models.enums import ProcessingMode, QualityMode
from ai3d.schemas.common import Schema


class HealthResponse(Schema):
    status: Literal["ok", "degraded"]
    version: str
    database: Literal["ok", "unavailable"]
    storage: Literal["ok", "unavailable"]
    time: datetime


class QualityModeInfo(Schema):
    id: QualityMode
    label: str
    description: str


class ProcessingModeInfo(Schema):
    id: ProcessingMode
    label: str
    description: str


class UploadLimits(Schema):
    max_upload_bytes: int
    max_images_per_project: int
    max_image_pixels: int
    accepted_mime_types: list[str]


class SystemInfo(Schema):
    version: str
    environment: str
    database_backend: str
    storage_backend: str
    data_dir: str | None
    storage_usage_bytes: int
    limits: UploadLimits
    quality_modes: list[QualityModeInfo]
    processing_modes: list[ProcessingModeInfo]
