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


class RemeshIn(Schema):
    """Rebuild the surface in Blender (kept only if it stays close to the original)."""

    method: Literal["quadriflow", "voxel"] = Field(
        description="quadriflow: an even quad layout; voxel: a uniform, closed surface."
    )
    target_faces: int = Field(
        default=20_000, ge=100, le=2_000_000, description="QuadriFlow: about this many quads."
    )
    resolution: int = Field(
        default=300, ge=16, le=2000, description="Voxel: cells across the model's diagonal."
    )
    maps: list[Literal["normal", "occlusion"]] = Field(
        default_factory=list,
        description="Extra maps to bake from the original onto the new surface.",
    )


class EnhanceRequest(Schema):
    quality_mode: QualityMode
    # Omitted = the model's current version. The source version is never changed.
    source_version_id: uuid.UUID | None = None
    remove_ground: bool = Field(
        default=False,
        description="Remove a supporting surface under the object (photogrammetry scans).",
    )
    scale: ScaleReferenceIn | None = None
    smoothing: int = Field(default=0, ge=0, le=50, description="Taubin smoothing passes (0 = off).")
    remesh: RemeshIn | None = None
    upscale_textures: Literal[2, 4] | None = Field(
        default=None,
        description="Enlarge base-colour textures ×2 or ×4 with the local AI upscaler "
        "(invents plausible detail; within the quality mode's texture size).",
    )


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


class RetextureRequest(Schema):
    """Ask a cloud service to make new textures for an existing version."""

    provider: str = Field(max_length=64)
    prompt: str = Field(
        default="", max_length=800, description="What the new texture should look like."
    )
    quality_mode: QualityMode = QualityMode.HIGH_QUALITY
    # Omitted = the model's current version. The source version is never changed.
    source_version_id: uuid.UUID | None = None
    cloud_upload_consent: bool = Field(
        default=False,
        description="Confirms the model may be uploaded to the provider. Without it the job "
        "waits for permission and nothing is sent.",
    )
