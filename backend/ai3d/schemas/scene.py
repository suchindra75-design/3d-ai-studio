import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from ai3d.models.enums import QualityMode
from ai3d.processing.scene_plan import (
    DEFAULT_SECONDS,
    MAX_SECONDS,
    MIN_SECONDS,
    Angle,
    Ground,
    Lighting,
    Motion,
    Shot,
    Style,
)
from ai3d.schemas.common import Schema
from ai3d.schemas.job import JobRead


class SceneChoices(Schema):
    """What the user picked; "auto" leaves it to the scene planner."""

    seconds: float = Field(default=DEFAULT_SECONDS, ge=MIN_SECONDS, le=MAX_SECONDS)
    quality_mode: QualityMode | None = Field(
        default=None, description="Omitted = the project's default quality."
    )
    shot: Shot | Literal["auto"] = "auto"
    angle: Angle | Literal["auto"] = "auto"
    lighting: Lighting | Literal["auto"] = "auto"
    ground: Ground | Literal["auto"] = "auto"
    style: Style | Literal["auto"] = "auto"


class SceneCreate(SceneChoices):
    prompt: str = Field(min_length=3, max_length=2000)
    send_to_gpu_server: bool = Field(
        default=False,
        description="The user agreed to send the prompt to their GPU server, which plans "
        "the scene and makes its objects. Required.",
    )


class SceneRenderAgain(SceneChoices):
    """Render the same objects again with another shot, light, set, look or length
    (on this computer only; nothing is sent to the GPU server)."""


class SceneObjectRead(Schema):
    name: str
    description: str
    size_m: float
    position_m: tuple[float, float] | None
    elevation_m: float
    facing_deg: float
    motion: Motion
    model_id: uuid.UUID | None
    model_version_id: uuid.UUID | None


class SceneFileRead(Schema):
    name: Literal["video", "still", "blend", "glb"]
    filename: str
    mime_type: str
    size_bytes: int
    url: str


class SceneRenderRead(Schema):
    frames: int
    fps: int
    width: int
    height: int
    engines: dict[str, str]
    seconds: dict[str, float]


class SceneRead(Schema):
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    prompt: str
    # "making" while its job runs, then "ready", "failed" or "cancelled".
    status: Literal["making", "ready", "failed", "cancelled"]
    job: JobRead | None
    source_scene_id: uuid.UUID | None
    style: Style
    lighting: Lighting
    ground: Ground
    shot: Shot
    angle: Angle
    lens_mm: float
    seconds: float
    quality_mode: QualityMode
    objects: list[SceneObjectRead]
    files: list[SceneFileRead]
    render: SceneRenderRead | None
    created_at: datetime
    updated_at: datetime


class SceneList(Schema):
    items: list[SceneRead]


class SceneReadiness(Schema):
    ready: bool
    # What to set up first, in plain words (GPU server, Blender).
    problems: list[str]
