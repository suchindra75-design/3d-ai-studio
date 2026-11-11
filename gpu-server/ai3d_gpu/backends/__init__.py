"""Model backends: what turns framed photos into a coloured mesh."""

from ai3d_gpu.backends.base import Backend, CancelledError, Generated, GenerationSettings, ViewPhoto
from ai3d_gpu.config import ServerConfig


def load_backend(config: ServerConfig) -> Backend:
    """The configured backend, loaded (TRELLIS loads its weights onto the GPU here)."""
    if config.backend == "stand-in":
        from ai3d_gpu.backends.stand_in import StandInBackend  # noqa: PLC0415

        return StandInBackend()
    from ai3d_gpu.backends.trellis import TrellisBackend  # noqa: PLC0415 - needs CUDA

    return TrellisBackend(config.model_id, config.model_revision)


__all__ = [
    "Backend",
    "CancelledError",
    "Generated",
    "GenerationSettings",
    "ViewPhoto",
    "load_backend",
]
