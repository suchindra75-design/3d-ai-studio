"""Application settings.

All settings come from environment variables prefixed with ``AI3D_`` or from a
``.env`` file in the working directory. Nothing here is ever sent to clients
verbatim; ``SystemInfo`` exposes a curated subset.
"""

from functools import cached_property
from pathlib import Path
from typing import Literal

from platformdirs import user_data_path
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "AI3DStudio"

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",  # Next.js dev server
    "http://127.0.0.1:3000",
    "http://localhost:1420",  # Tauri/Vite dev server
    "http://127.0.0.1:1420",
    "tauri://localhost",  # Tauri production (macOS/Linux)
    "http://tauri.localhost",  # Tauri production (Windows)
]


def _default_data_dir() -> Path:
    return user_data_path(APP_NAME, appauthor=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AI3D_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "production", "test"] = "development"

    # Where projects, uploads, generated models and the SQLite database live.
    data_dir: Path = Field(default_factory=_default_data_dir)
    # Overrides the default SQLite database inside data_dir, e.g.
    # postgresql+psycopg://user:pass@host/db
    database_url: str | None = None
    auto_migrate: bool = True

    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: list(DEFAULT_CORS_ORIGINS))
    # Host headers the API answers to. Guards against DNS-rebinding attacks from
    # web pages; add your server's hostname when exposing it (or "*" to disable).
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "::1", "[::1]"]
    )
    # Absolute filesystem paths are shown in Settings for local installs; hide
    # them on shared/self-hosted servers.
    expose_paths: bool = True

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # Upload limits
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_images_per_project: int = Field(default=300, gt=0)
    # Pillow decompression-bomb guard (pixels). 150 MP covers medium format.
    max_image_pixels: int = Field(default=150_000_000, gt=0)
    thumbnail_size: int = Field(default=512, ge=64, le=2048)

    # When set, every API request must present this token (the desktop app sets a
    # fresh random one for each launch of its private backend). Unset for the web app.
    api_token: SecretStr | None = Field(default=None, min_length=16)
    # The desktop app's process id: the backend shuts down when that process is gone
    # (e.g. the app was force-quit), instead of running on in the background.
    parent_pid: int | None = Field(default=None, gt=0)
    # Optional error reporting to a Sentry-compatible service you choose (needs the
    # `errors` extra). Off when unset. Only errors are sent, scrubbed; see
    # docs/ERROR_REPORTING.md.
    sentry_dsn: SecretStr | None = None

    # Where photos, models and exports are stored: the data folder ("local"), or an
    # S3-compatible bucket ("s3", needs the `s3` extra). The database stays separate.
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str | None = None
    s3_prefix: str = ""
    s3_endpoint_url: str | None = None  # e.g. http://127.0.0.1:9000 for MinIO
    s3_region: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None

    # Encrypts provider API keys entered in the UI. When unset, a random key is
    # generated once and kept in ``<data_dir>/secret.key`` (mode 0600).
    secret_key: SecretStr | None = None

    # Cloud provider keys. Environment keys take precedence over keys stored
    # through the UI and are never returned by the API.
    tripo_api_key: SecretStr | None = None
    meshy_api_key: SecretStr | None = None
    tripo_base_url: str = "https://openapi.tripo3d.ai/v3"
    meshy_base_url: str = "https://api.meshy.ai/openapi/v1"

    # Background jobs (run in-process by default).
    run_jobs: bool = True
    max_concurrent_jobs: int = Field(default=2, ge=1, le=16)
    # Seconds between provider status checks while a generation runs.
    provider_poll_interval: float = Field(default=3.0, ge=0)
    # A generation that has not finished after this many seconds fails.
    job_timeout_seconds: int = Field(default=3600, ge=10)
    # Seconds a worker may hold a job without a heartbeat before it is recovered.
    job_lease_seconds: int = Field(default=60, ge=5)
    # Downloaded 3D files larger than this are rejected.
    max_model_bytes: int = Field(default=500 * 1024 * 1024, gt=0)
    # Longest side of the photo copies sent to providers.
    provider_image_max_side: int = Field(default=2048, ge=256, le=8192)

    # Local photogrammetry. COLMAP is found on PATH unless a path is given here.
    colmap_path: Path | None = None
    # OpenMVS programs (dense photogrammetry on the CPU); found on PATH or in the usual
    # install folders unless a folder is given here.
    openmvs_dir: Path | None = None
    # The optional local AI engine (TripoSR), installed with `ai3d-local-ai install`.
    # Default: ~/.local/share/ai3d-studio/tools/local-ai
    local_ai_dir: Path | None = None
    local_ai_timeout_seconds: int = Field(default=30 * 60, ge=60)
    # A local reconstruction that has not finished after this many seconds fails.
    reconstruction_timeout_seconds: int = Field(default=4 * 3600, ge=60)

    @field_validator("data_dir")
    @classmethod
    def _expand_data_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @cached_property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"

    @cached_property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'ai3d.sqlite3'}"

    @property
    def enable_mock_provider(self) -> bool:
        """The mock provider fakes generation and exists only for automated tests."""
        return self.environment == "test"

    @property
    def database_backend(self) -> str:
        return self.resolved_database_url.split(":", 1)[0].split("+", 1)[0]

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
