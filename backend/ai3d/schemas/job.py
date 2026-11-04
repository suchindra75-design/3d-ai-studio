import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ai3d.models.enums import (
    JobEventType,
    JobKind,
    JobStatus,
    ProcessingMode,
    ProviderKind,
    QualityMode,
)
from ai3d.schemas.common import APIModel, ErrorBody, Schema

ViewRoleField = Literal["source", "front", "left", "back", "right"]


class GenerationInput(Schema):
    image_id: uuid.UUID
    role: ViewRoleField = Field(
        default="source",
        description="`source` for a single photo; `front`/`left`/`back`/`right` for "
        "multi-view generation (the front view is required).",
    )


class GenerationCreate(Schema):
    # Up to 4 for AI multi-view, many more for photogrammetry (checked per engine).
    inputs: list[GenerationInput] = Field(min_length=1, max_length=300)
    provider: str | None = Field(
        default=None, description="Provider key; omitted = choose automatically."
    )
    quality_mode: QualityMode | None = None
    processing_mode: ProcessingMode | None = None
    cloud_upload_consent: bool = Field(
        default=False,
        description="The user explicitly agreed to send these photos to the provider. "
        "Without it (or when the project keeps photos local) the job waits for consent.",
    )


class ConsentGrant(Schema):
    allow_project_cloud_upload: bool = Field(
        default=False,
        description="Also allow cloud upload for the whole project. Required when the "
        "project is set to keep photos local.",
    )


class RetryRequest(Schema):
    cloud_upload_consent: bool = False
    # Run the same photos with another engine (one of the failed job's alternatives).
    provider: str | None = Field(default=None, max_length=64)


class CostEstimateRead(Schema):
    amount: float
    unit: str
    usd: float | None
    basis: str
    notes: list[str] = Field(default_factory=list)


class JobCostRead(Schema):
    credits: float | None = None
    usd: float | None = None
    source: str = "provider"


class ProviderSummary(Schema):
    key: str
    display_name: str
    kind: ProviderKind
    uploads_images: bool


class JobAlternative(Schema):
    """Another engine that can do the same job, offered when a job failed."""

    provider: ProviderSummary
    reason: str


class PlanIssue(Schema):
    code: str
    message: str


class PlannedInputRead(Schema):
    image_id: uuid.UUID
    role: str
    filename: str
    quality_level: str


class GenerationPlanRead(Schema):
    ok: bool
    kind: JobKind | None
    quality_mode: QualityMode
    processing_mode: ProcessingMode
    route: Literal["cloud", "local"] | None
    provider: ProviderSummary | None
    inputs: list[PlannedInputRead]
    provider_settings: dict[str, Any]
    provider_summary: list[str]
    estimate: CostEstimateRead | None
    requires_consent: bool
    consent_reason: Literal["project_local_only", "confirmation_required"] | None
    provenance: Literal["inferred", "measured"] | None
    warnings: list[str]
    blocking_issues: list[PlanIssue]


class JobInputRead(APIModel):
    position: int
    image_id: uuid.UUID | None
    image_sha256: str
    role: str


class JobRead(Schema):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: JobKind
    status: JobStatus
    stage: str | None
    progress: float
    quality_mode: QualityMode
    processing_mode: ProcessingMode
    provider: ProviderSummary | None
    provider_task_id: str | None
    inputs: list[JobInputRead]
    provider_settings: dict[str, Any]
    provider_summary: list[str]
    estimate: CostEstimateRead | None
    cost: JobCostRead | None
    warnings: list[str]
    cloud_upload_consented: bool
    consent_reason: str | None
    cancel_requested: bool
    error: ErrorBody | None
    attempt: int
    parent_job_id: uuid.UUID | None
    result_model_version_id: uuid.UUID | None
    # Enhance jobs: the version that was processed.
    source_model_version_id: uuid.UUID | None = None
    result_model_id: uuid.UUID | None
    can_cancel: bool
    can_retry: bool
    # Failed jobs: other engines for the same photos (local first). Cloud ones still ask
    # before uploading; jobs created in Local mode are only offered local engines.
    alternatives: list[JobAlternative] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class JobList(Schema):
    items: list[JobRead]
    total: int


class JobEventRead(APIModel):
    seq: int
    type: JobEventType
    stage: str | None
    message: str
    progress: float | None
    data: dict[str, Any] | None
    created_at: datetime


class JobEventList(Schema):
    items: list[JobEventRead]
    job_status: JobStatus
