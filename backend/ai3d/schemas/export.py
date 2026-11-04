import uuid
from datetime import datetime
from typing import Any

from pydantic import computed_field

from ai3d.models.enums import ExportFormat, ExportStatus
from ai3d.schemas.common import Schema

_API = "/api/v1"


class ExportFormatRead(Schema):
    format: ExportFormat
    label: str
    available: bool
    unavailable_reason: str | None
    keeps_texture: bool
    keeps_colour: bool  # per-vertex colours (e.g. local AI models)
    notes: list[str]


class ExportFormatList(Schema):
    items: list[ExportFormatRead]


class ExportCreate(Schema):
    format: ExportFormat


class ExportRead(Schema):
    id: uuid.UUID
    model_version_id: uuid.UUID
    format: ExportFormat
    status: ExportStatus
    # What re-opening the file found (triangles, size, units, texture).
    validation: dict[str, Any] | None
    notes: list[str]
    error_message: str | None
    size_bytes: int | None
    created_at: datetime
    completed_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def file_url(self) -> str | None:
        return f"{_API}/exports/{self.id}/file" if self.status == ExportStatus.READY else None


class ExportList(Schema):
    items: list[ExportRead]
