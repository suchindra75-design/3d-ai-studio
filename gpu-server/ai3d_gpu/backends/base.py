"""The contract between the job queue and the model backends: 3D shapes from
photos, text (scene plans) from a prompt, and images from a prompt."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from ai3d_gpu.framing import Framed

Role = Literal["source", "front", "left", "back", "right"]


class CancelledError(Exception):
    """Raised by a backend when the job was cancelled between steps."""


class GenerationSettings(BaseModel):
    """What the app may ask for. Defaults are TRELLIS-image-large's own
    (its pipeline.json: 25 steps at guidance 5 for both stages)."""

    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    structure_steps: int = Field(default=25, ge=1, le=100)
    structure_guidance: float = Field(default=5.0, ge=0.0, le=20.0)
    detail_steps: int = Field(default=25, ge=1, le=100)
    detail_guidance: float = Field(default=5.0, ge=0.0, le=20.0)
    # With several photos: "stochastic" (one photo per step) or "multidiffusion".
    multi_view_mode: Literal["stochastic", "multidiffusion"] = "stochastic"


@dataclass(frozen=True, slots=True)
class ViewPhoto:
    role: Role
    image: Image.Image  # as uploaded (RGB, or RGBA with its own transparency)


@dataclass(slots=True)
class Generated:
    """glTF axes (Y up). ``colours``: per vertex, sRGB 0..1. The front is roughly but
    not exactly towards +Z; the app finds the exact turn from the photo."""

    vertices: np.ndarray
    faces: np.ndarray
    colours: np.ndarray | None
    framed: Mapping[str, Framed]  # per role: the photo framed as the model saw it
    info: dict[str, Any] = field(default_factory=dict)


Progress = Callable[[str, float], None]


class Backend(Protocol):
    name: str
    model: str
    device: str

    def generate(
        self,
        views: list[ViewPhoto],
        settings: GenerationSettings,
        progress: Progress,
        cancelled: Callable[[], bool],
    ) -> Generated: ...


class TextSettings(BaseModel):
    """Sampling for the text model (Qwen3-Instruct's recommended defaults)."""

    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    max_tokens: int = Field(default=1500, ge=16, le=4096)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.8, gt=0.0, le=1.0)


class TextRequest(BaseModel):
    """A system instruction and the user's prompt; the reply is plain text."""

    system: str = Field(default="", max_length=20_000)
    prompt: str = Field(min_length=1, max_length=4_000)
    settings: TextSettings = Field(default_factory=TextSettings)


class ImageSettings(BaseModel):
    """FLUX.1-schnell is distilled for 1–4 steps without guidance."""

    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    width: int = Field(default=1024, ge=256, le=1536, multiple_of=16)
    height: int = Field(default=1024, ge=256, le=1536, multiple_of=16)
    steps: int = Field(default=4, ge=1, le=50)


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2_000)
    settings: ImageSettings = Field(default_factory=ImageSettings)


class TextBackend(Protocol):
    name: str
    model: str
    device: str

    def write(self, request: TextRequest, cancelled: Callable[[], bool]) -> str: ...


class ImageBackend(Protocol):
    name: str
    model: str
    device: str

    def paint(
        self, request: ImageRequest, progress: Progress, cancelled: Callable[[], bool]
    ) -> Image.Image: ...
