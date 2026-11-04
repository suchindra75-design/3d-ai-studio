import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ai3d.database.base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)
from ai3d.models.enums import JobEventType, JobKind, JobStatus, ProcessingMode, QualityMode

if TYPE_CHECKING:
    from ai3d.models.project import Project


class GenerationJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A persistent unit of background work (generation, reconstruction, export…).

    The worker claims a job by setting ``lease_owner``/``lease_expires_at``; a
    job whose lease expired is recovered on start-up.
    """

    __tablename__ = "generation_jobs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[JobKind] = mapped_column(str_enum(JobKind))
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.QUEUED)
    quality_mode: Mapped[QualityMode] = mapped_column(str_enum(QualityMode))
    processing_mode: Mapped[ProcessingMode] = mapped_column(str_enum(ProcessingMode))

    provider_key: Mapped[str | None] = mapped_column(String(64))
    provider_config_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("provider_configurations.id", ondelete="SET NULL")
    )
    # Provider-side task id, stored as soon as it exists so a restart can
    # resume polling instead of paying for a second generation.
    provider_task_id: Mapped[str | None] = mapped_column(String(255))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)

    stage: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[float] = mapped_column(default=0.0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_details: Mapped[dict[str, Any] | None]

    attempt: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL")
    )
    source_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL")
    )
    result_model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL")
    )

    # Explicit per-job consent to send images to an external provider.
    cloud_upload_consented: Mapped[bool] = mapped_column(default=False)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    # Usage/cost exactly as reported by the provider (credits, currency…).
    cost: Mapped[dict[str, Any] | None]

    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None]
    queued_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    project: Mapped["Project"] = relationship(back_populates="jobs")
    inputs: Mapped[list["JobInput"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="JobInput.position"
    )
    events: Mapped[list["JobEvent"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True, order_by="JobEvent.seq"
    )

    __table_args__ = (
        Index("ix_generation_jobs_status_queued_at", "status", "queued_at"),
        Index("ix_generation_jobs_lease_expires_at", "lease_expires_at"),
    )


class JobInput(Base):
    """Ordered input images of a job."""

    __tablename__ = "job_inputs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(primary_key=True)
    # SET NULL keeps job history readable after an image is deleted.
    image_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("images.id", ondelete="SET NULL"), index=True
    )
    image_sha256: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32), default="source")


class JobEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only job timeline; streamed over SSE (``seq`` = event id)."""

    __tablename__ = "job_events"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"))
    seq: Mapped[int]
    type: Mapped[JobEventType] = mapped_column(str_enum(JobEventType))
    stage: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    progress: Mapped[float | None]
    data: Mapped[dict[str, Any] | None]

    __table_args__ = (UniqueConstraint("job_id", "seq"),)
