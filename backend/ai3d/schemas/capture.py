import uuid
from typing import Literal

from pydantic import Field

from ai3d.models.enums import InputMode
from ai3d.schemas.common import Schema

CheckStatus = Literal["pass", "warning", "fail", "not_assessed"]


class CaptureCheck(Schema):
    code: str
    status: CheckStatus
    title: str
    message: str
    image_ids: list[uuid.UUID] = Field(default_factory=list)
    # Something the user can run to complete this check.
    action: Literal["measure_overlap"] | None = None


class CaptureAssessment(Schema):
    """Set-level readiness of a project's photos for reconstruction."""

    input_mode: InputMode
    image_count: int
    min_images: int
    recommended_images: int
    readiness: Literal["ready", "needs_attention", "insufficient"]
    summary: str
    checks: list[CaptureCheck]
