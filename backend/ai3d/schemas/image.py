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


class CutoutRead(Schema):
    """What local generation's background removal makes of the photo (on request)."""

    background_removed: bool = Field(
        description="False when the photo's own transparency is used instead."
    )
    coverage: float = Field(description="Share of the photo inside the cut-out.")
    touches: list[Literal["top", "bottom", "left", "right"]] = Field(
        default_factory=list, description="Photo edges the object runs off."
    )
    background_match: float | None = Field(
        default=None,
        description="Share of the colours along the object's edge that also occur in the "
        "background beside it (high = the object blends in).",
    )
    warnings: list[QualityWarningRead] = Field(default_factory=list)
    checked_at: datetime


class ImageAnalysisRead(Schema):
    sharpness: float
    brightness: float
    contrast: float
    clipped_highlights: float
    clipped_shadows: float
    megapixels: float
    subject_coverage: float | None = None
    warnings: list[QualityWarningRead] = Field(default_factory=list)
    cutout: CutoutRead | None = None


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cutout_url(self) -> str | None:
        """The picture local AI generation receives (after a cut-out check)."""
        cutout = self.analysis.cutout
        if cutout is None:
            return None
        return f"/api/v1/images/{self.id}/cutout?v={int(cutout.checked_at.timestamp())}"


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
