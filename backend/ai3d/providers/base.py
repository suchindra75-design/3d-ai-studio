"""The provider interface.

A provider turns photos into a 3D file: a cloud API (Tripo, Meshy), a local
engine (later phases) or the test-only mock. The job pipeline talks to
providers exclusively through :class:`Provider`; it never imports a concrete
adapter. Adapters are synchronous and run on worker threads.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import httpx2

from ai3d.models.enums import JobKind, ProviderKind, QualityMode

if TYPE_CHECKING:
    from ai3d.core.config import Settings

ViewRole = Literal["source", "front", "left", "back", "right"]
VIEW_ORDER: tuple[ViewRole, ...] = ("front", "left", "back", "right")

TaskState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
CancelOutcome = Literal["cancelled", "unsupported", "too_late"]
CancelSupport = Literal["supported", "pending_only", "unsupported"]

DEFAULT_MAX_MODEL_BYTES = 500 * 1024 * 1024


class ProviderErrorCode(StrEnum):
    AUTH_FAILED = "auth_failed"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    RATE_LIMITED = "rate_limited"
    INVALID_INPUT = "invalid_input"
    CONTENT_REJECTED = "content_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TASK_FAILED = "task_failed"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"


_TRANSIENT = {
    ProviderErrorCode.RATE_LIMITED,
    ProviderErrorCode.PROVIDER_UNAVAILABLE,
    ProviderErrorCode.TIMEOUT,
}


class ProviderError(Exception):
    """A provider failure normalised to one of :class:`ProviderErrorCode`."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = code in _TRANSIENT if retryable is None else retryable
        self.retry_after = retry_after
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class MultiViewLimits:
    min_images: int
    max_images: int
    required_roles: tuple[ViewRole, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    single_image: bool
    multi_view: MultiViewLimits | None
    accepted_mime_types: tuple[str, ...]
    max_input_bytes: int
    output_formats: tuple[str, ...]
    supports_texture: bool
    supports_pbr: bool
    cancel: CancelSupport
    reports_progress: bool
    notes: tuple[str, ...] = ()
    # Measured reconstruction from many overlapping photos (local engines).
    photogrammetry: MultiViewLimits | None = None
    # Can re-texture an existing model (JobKind.RETEXTURE); the model is uploaded.
    retexture: bool = False
    # Only models this provider generated itself can be re-textured.
    retexture_own_models_only: bool = False

    def supports(self, kind: JobKind) -> bool:
        if kind == JobKind.IMAGE_TO_3D:
            return self.single_image
        if kind == JobKind.MULTI_VIEW_TO_3D:
            return self.multi_view is not None
        if kind == JobKind.PHOTOGRAMMETRY:
            return self.photogrammetry is not None
        if kind == JobKind.RETEXTURE:
            return self.retexture
        return False


@dataclass(frozen=True, slots=True)
class CostEstimate:
    amount: float
    unit: str
    usd: float | None
    basis: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderPlan:
    """How a quality profile translates into this provider's own settings."""

    settings: dict[str, Any]
    summary: tuple[str, ...]
    estimate: CostEstimate | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderInput:
    path: Path
    mime_type: str
    role: ViewRole
    sha256: str


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    job_id: uuid.UUID
    kind: JobKind
    quality_mode: QualityMode
    inputs: tuple[ProviderInput, ...]
    settings: dict[str, Any]


@dataclass(slots=True)
class TaskStatus:
    state: TaskState
    progress: float | None = None  # 0..1 when the provider reports it
    message: str = ""
    error: ProviderError | None = None
    # Usage exactly as reported by the provider.
    cost: dict[str, Any] | None = None
    # Short-lived download links. Never persisted or logged (they are signed URLs).
    outputs: dict[str, str] = field(default_factory=dict)
    # Non-sensitive provider fields kept for diagnostics.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class ProviderResult:
    mesh_path: Path
    mesh_format: str
    preview_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CredentialCheck:
    ok: bool
    message: str
    balance: float | None = None
    unit: str | None = None


class Provider(ABC):
    key: ClassVar[str]
    display_name: ClassVar[str]
    kind: ClassVar[ProviderKind]
    description: ClassVar[str]
    # True when generation sends the user's photos to a third party.
    uploads_images: ClassVar[bool]
    homepage_url: ClassVar[str | None] = None
    docs_url: ClassVar[str | None] = None
    pricing_url: ClassVar[str | None] = None
    # Settings attribute holding the environment key, e.g. "tripo_api_key".
    api_key_setting: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        transport: httpx2.BaseTransport | None = None,
        max_model_bytes: int = DEFAULT_MAX_MODEL_BYTES,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._transport = transport
        self._max_model_bytes = max_model_bytes

    @classmethod
    @abstractmethod
    def capabilities(cls) -> ProviderCapabilities: ...

    @classmethod
    def unavailable_reason(cls, settings: "Settings") -> str | None:
        """Why the provider cannot run on this installation (e.g. a missing local
        tool), or None. Cloud providers are always available; their API key is
        checked separately."""
        return None

    @classmethod
    @abstractmethod
    def plan(cls, kind: JobKind, quality_mode: QualityMode) -> ProviderPlan:
        """Translate a quality profile into provider settings and a cost estimate."""

    @abstractmethod
    def submit(self, request: GenerationRequest) -> str:
        """Start a task and return the provider's task reference."""

    @abstractmethod
    def poll(self, task_id: str) -> TaskStatus: ...

    @abstractmethod
    def cancel(self, task_id: str) -> CancelOutcome: ...

    @abstractmethod
    def download_result(self, task_id: str, status: TaskStatus, dest_dir: Path) -> ProviderResult:
        """Download the finished model (and a preview image if offered) into ``dest_dir``."""

    @abstractmethod
    def check_credentials(self) -> CredentialCheck: ...

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""
