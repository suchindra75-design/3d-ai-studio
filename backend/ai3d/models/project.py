from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ai3d.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from ai3d.models.enums import InputMode, ProcessingMode, ProjectStatus, QualityMode

if TYPE_CHECKING:
    from ai3d.models.image import Image
    from ai3d.models.job import GenerationJob
    from ai3d.models.model3d import Model3D


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ProjectStatus] = mapped_column(
        str_enum(ProjectStatus), default=ProjectStatus.ACTIVE
    )
    input_mode: Mapped[InputMode] = mapped_column(str_enum(InputMode))
    default_quality_mode: Mapped[QualityMode] = mapped_column(
        str_enum(QualityMode), default=QualityMode.FAST
    )
    default_processing_mode: Mapped[ProcessingMode] = mapped_column(
        str_enum(ProcessingMode), default=ProcessingMode.AUTO
    )
    # Images of this project may be sent to external providers only when true.
    allow_cloud_upload: Mapped[bool] = mapped_column(default=False)
    archived_at: Mapped[datetime | None]

    images: Mapped[list["Image"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Image.position",
    )
    jobs: Mapped[list["GenerationJob"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    models: Mapped[list["Model3D"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("ix_projects_status_updated_at", "status", "updated_at"),)
