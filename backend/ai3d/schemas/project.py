import uuid
from datetime import datetime

from pydantic import Field, field_validator

from ai3d.models.enums import InputMode, ProcessingMode, ProjectStatus, QualityMode
from ai3d.schemas.common import APIModel, PageMeta


def _strip(value: str) -> str:
    return value.strip()


class ProjectCreate(APIModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    input_mode: InputMode
    default_quality_mode: QualityMode = QualityMode.FAST
    default_processing_mode: ProcessingMode = ProcessingMode.AUTO
    allow_cloud_upload: bool = False

    _strip_name = field_validator("name", "description")(_strip)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("name must not be blank")
        return value


class ProjectUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    status: ProjectStatus | None = None
    default_quality_mode: QualityMode | None = None
    default_processing_mode: ProcessingMode | None = None
    allow_cloud_upload: bool | None = None

    @field_validator("name", "description")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ProjectRead(APIModel):
    id: uuid.UUID
    name: str
    description: str
    status: ProjectStatus
    input_mode: InputMode
    default_quality_mode: QualityMode
    default_processing_mode: ProcessingMode
    allow_cloud_upload: bool
    image_count: int = 0
    total_image_bytes: int = 0
    cover_image_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class ProjectList(PageMeta):
    items: list[ProjectRead]
