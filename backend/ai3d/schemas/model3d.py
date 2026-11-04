import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, computed_field, field_validator

from ai3d.models.enums import (
    ModelVersionSource,
    QualityMode,
    ReportStatus,
    SurfaceProvenance,
)
from ai3d.schemas.common import APIModel, Schema

_API = "/api/v1"


class QualityIssueRead(Schema):
    code: str
    severity: str
    message: str


class QualityReportRead(APIModel):
    status: ReportStatus
    report_version: int
    metrics: dict[str, Any]
    issues: list[QualityIssueRead]
    created_at: datetime


class MeshFileRead(Schema):
    format: str
    mime_type: str
    size_bytes: int
    sha256: str


class ModelVersionRead(Schema):
    id: uuid.UUID
    model_id: uuid.UUID
    version_number: int
    parent_version_id: uuid.UUID | None
    source: ModelVersionSource
    quality_mode: QualityMode | None
    provider_key: str | None
    provider_display_name: str | None
    surface_provenance: SurfaceProvenance
    stats: dict[str, Any]
    notes: str
    job_id: uuid.UUID | None
    mesh: MeshFileRead | None
    has_preview: bool
    quality_report: QualityReportRead | None
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def file_url(self) -> str | None:
        return f"{_API}/model-versions/{self.id}/file" if self.mesh else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preview_url(self) -> str | None:
        return f"{_API}/model-versions/{self.id}/preview" if self.has_preview else None


class ModelRead(Schema):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    created_at: datetime
    updated_at: datetime
    current_version: ModelVersionRead | None
    version_count: int


class ModelList(Schema):
    items: list[ModelRead]
    total: int


class ModelVersionList(Schema):
    items: list[ModelVersionRead]
    current_version_id: uuid.UUID | None


class ModelUpdate(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    # Switching versions never deletes one; every version stays downloadable.
    current_version_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None
