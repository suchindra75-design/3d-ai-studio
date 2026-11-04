import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from ai3d.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, str_enum
from ai3d.models.enums import ExportFormat, ExportStatus


class Export(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A conversion of a model version to a file format, validated by re-opening it."""

    __tablename__ = "exports"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), index=True
    )
    format: Mapped[ExportFormat] = mapped_column(str_enum(ExportFormat))
    status: Mapped[ExportStatus] = mapped_column(
        str_enum(ExportStatus), default=ExportStatus.PENDING
    )
    options: Mapped[dict[str, Any]] = mapped_column(default=dict)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL")
    )
    # Result of re-opening the exported file (counts, bbox, warnings).
    validation: Mapped[dict[str, Any] | None]
    error_message: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None]
