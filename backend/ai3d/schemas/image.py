import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, computed_field

from ai3d.models.enums import ImageKind, QualityLevel
from ai3d.schemas.common import APIModel, ErrorBody, Schema


class QualityWarningRead(Schema):
    code: str
    severity: Literal["info", "warning", "error"]
    message: str


class ImageAnalysisRead(Schema):
    sharpness: float
    brightness: float
    contrast: float
    clipped_highlights: float
    clipped_shadows: float
    megapixels: float
    subject_coverage: float | None = None
    warnings: list[QualityWarningRead] = Field(default_factory=list)


class ImageRead(APIModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: ImageKind
    position: int
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    exif_orientation: int | None
    camera_make: str | None
    camera_model: str | None
    focal_length_mm: float | None
    focal_length_35mm: float | None
    exif: dict[str, Any]
    quality_level: QualityLevel
    analysis: ImageAnalysisRead
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def file_url(self) -> str:
        return f"/api/v1/images/{self.id}/file"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def thumbnail_url(self) -> str:
        return f"/api/v1/images/{self.id}/thumbnail"


class ImageList(Schema):
    items: list[ImageRead]
    total: int


class UploadItemResult(Schema):
    filename: str
    status: Literal["created", "duplicate", "rejected"]
    image: ImageRead | None = None
    error: ErrorBody | None = None


class UploadResponse(Schema):
    results: list[UploadItemResult]
    created: int
    duplicates: int
    rejected: int
