import uuid
from typing import Literal

from pydantic import Field

from ai3d.models.enums import QualityMode
from ai3d.schemas.common import Schema


class ScaleReferenceIn(Schema):
    """A real measurement of the object, used to give the model a real-world scale."""

    axis: Literal["x", "y", "z", "longest"] = Field(
        description="Which extent was measured: width (x), height (y), depth (z) or the longest."
    )
    length_mm: float = Field(gt=0, le=100_000, description="The measured length in millimetres.")


class EnhanceRequest(Schema):
    quality_mode: QualityMode
    # Omitted = the model's current version. The source version is never changed.
    source_version_id: uuid.UUID | None = None
    remove_ground: bool = Field(
        default=False,
        description="Remove a supporting surface under the object (photogrammetry scans).",
    )
    scale: ScaleReferenceIn | None = None


class EnhanceStepPlan(Schema):
    code: str
    title: str
    # will_run: always attempted (it may still find nothing to do);
    # unavailable: needs a tool this installation does not have.
    status: Literal["will_run", "unavailable"]
    detail: str


class EnhancePlanRead(Schema):
    quality_mode: QualityMode
    steps: list[EnhanceStepPlan]
    notes: list[str]
