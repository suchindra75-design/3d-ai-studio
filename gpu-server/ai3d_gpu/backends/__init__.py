"""Model backends: 3D shapes from photos, and (for scene prompts) text and images."""

from dataclasses import dataclass

from ai3d_gpu.backends.base import (
    Backend,
    CancelledError,
    Generated,
    GenerationSettings,
    ImageBackend,
    ImageRequest,
    TextBackend,
    TextRequest,
    ViewPhoto,
)
from ai3d_gpu.config import ServerConfig


@dataclass(frozen=True, slots=True)
class Engines:
    shape: Backend
    text: TextBackend | None = None
    image: ImageBackend | None = None


def load_backend(config: ServerConfig) -> Backend:
    """The configured 3D backend, loaded (TRELLIS loads its weights onto the GPU here)."""
    if config.backend == "stand-in":
        from ai3d_gpu.backends.stand_in import StandInBackend  # noqa: PLC0415

        return StandInBackend()
    from ai3d_gpu.backends.trellis import TrellisBackend  # noqa: PLC0415 - needs CUDA

    return TrellisBackend(config.model_id, config.model_revision)


def load_engines(config: ServerConfig) -> Engines:
    """All configured backends. The text and image models load on first use."""
    shape = load_backend(config)
    if not config.scenes:
        return Engines(shape)
    if config.backend == "stand-in":
        from ai3d_gpu.backends.stand_in import StandInImage, StandInText  # noqa: PLC0415

        return Engines(shape, StandInText(), StandInImage())
    from ai3d_gpu.backends.flux import FluxImages  # noqa: PLC0415
    from ai3d_gpu.backends.qwen import QwenText  # noqa: PLC0415

    return Engines(
        shape,
        QwenText(config.text_model_id, config.text_model_revision),
        FluxImages(config.image_model_id, config.image_model_revision),
    )


__all__ = [
    "Backend",
    "CancelledError",
    "Engines",
    "Generated",
    "GenerationSettings",
    "ImageBackend",
    "ImageRequest",
    "TextBackend",
    "TextRequest",
    "ViewPhoto",
    "load_backend",
    "load_engines",
]
