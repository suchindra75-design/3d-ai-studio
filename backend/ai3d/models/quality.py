import uuid
from typing import Any

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from ai3d.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, str_enum
from ai3d.models.enums import ReportStatus


class QualityReport(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Mesh/texture validation results for one model version."""

    __tablename__ = "quality_reports"

    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL")
    )
    status: Mapped[ReportStatus] = mapped_column(str_enum(ReportStatus))
    # Bumped whenever the shape of ``metrics``/``issues`` changes.
    report_version: Mapped[int] = mapped_column(default=1)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    issues: Mapped[list[Any]] = mapped_column(default=list)
