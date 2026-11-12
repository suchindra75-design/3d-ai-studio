"""Server settings, from environment variables (``AI3D_GPU_*``)."""

import os
from dataclasses import dataclass
from pathlib import Path

MIN_TOKEN_LENGTH = 24
DEFAULT_REVISION = "25e0d31ffbebe4b5a97464dd851910efc3002d96"
# Scene prompts (both Apache-2.0), pinned to the revisions this server was written against.
TEXT_MODEL = "Qwen/Qwen3-4B-Instruct-2507"
TEXT_REVISION = "cdbee75f17c01a7cc42f958dc650907174af0554"
IMAGE_MODEL = "black-forest-labs/FLUX.1-schnell"
IMAGE_REVISION = "741f7c3ce8b383c54771c7003378a50191e9efe9"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ServerConfig:
    token: str
    backend: str = "trellis"  # "trellis" or "stand-in" (no GPU; for trying the plumbing)
    work_dir: Path = Path("/tmp/ai3d-gpu")  # noqa: S108 - inside the container
    max_images: int = 4
    max_upload_bytes: int = 20 * 1024 * 1024  # per photo
    max_queue: int = 8
    keep_seconds: int = 24 * 3600  # finished jobs are deleted after this
    model_id: str = "microsoft/TRELLIS-image-large"
    # Pinned: the weights this server was written against (MIT licence).
    model_revision: str | None = DEFAULT_REVISION
    # Prompt-to-scene models (loaded on first use); off: the server makes 3D models only.
    scenes: bool = True
    text_model_id: str = TEXT_MODEL
    text_model_revision: str | None = TEXT_REVISION
    image_model_id: str = IMAGE_MODEL
    image_model_revision: str | None = IMAGE_REVISION

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ServerConfig":
        env = dict(os.environ if env is None else env)
        token = env.get("AI3D_GPU_TOKEN", "").strip()
        if len(token) < MIN_TOKEN_LENGTH or any(ch.isspace() for ch in token):
            raise ConfigError(
                f"Set AI3D_GPU_TOKEN to a random secret of at least {MIN_TOKEN_LENGTH} "
                "characters (for example: openssl rand -hex 32). The app sends it with "
                "every request; without it anyone who finds the server could use the GPU."
            )
        backend = env.get("AI3D_GPU_BACKEND", "trellis")
        if backend not in {"trellis", "stand-in"}:
            raise ConfigError("AI3D_GPU_BACKEND must be 'trellis' or 'stand-in'.")
        return cls(
            token=token,
            backend=backend,
            work_dir=Path(env.get("AI3D_GPU_WORK_DIR", "/tmp/ai3d-gpu")),  # noqa: S108
            max_queue=int(env.get("AI3D_GPU_MAX_QUEUE", "8")),
            keep_seconds=int(env.get("AI3D_GPU_KEEP_HOURS", "24")) * 3600,
            model_id=env.get("AI3D_GPU_MODEL", "microsoft/TRELLIS-image-large"),
            model_revision=env.get("AI3D_GPU_MODEL_REVISION") or DEFAULT_REVISION,
            scenes=env.get("AI3D_GPU_SCENES", "on").lower() not in {"off", "0", "false", "no"},
            text_model_id=env.get("AI3D_GPU_TEXT_MODEL", TEXT_MODEL),
            text_model_revision=env.get("AI3D_GPU_TEXT_MODEL_REVISION") or TEXT_REVISION,
            image_model_id=env.get("AI3D_GPU_IMAGE_MODEL", IMAGE_MODEL),
            image_model_revision=env.get("AI3D_GPU_IMAGE_MODEL_REVISION") or IMAGE_REVISION,
        )
