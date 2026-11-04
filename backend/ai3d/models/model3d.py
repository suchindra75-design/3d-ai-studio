import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ai3d.database.base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    str_enum,
)
from ai3d.models.enums import ModelVersionSource, QualityMode, SurfaceProvenance

if TYPE_CHECKING:
    from ai3d.models.project import Project


class Model3D(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A logical 3D model; its geometry lives in immutable versions."""

    __tablename__ = "models"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    # Circular reference (models <-> model_versions) is added after both tables exist.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL", use_alter=True)
    )

    project: Mapped["Project"] = relationship(back_populates="models")
    versions: Mapped[list["ModelVersion"]] = relationship(
        back_populates="model",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="ModelVersion.model_id",
        order_by="ModelVersion.version_number",
    )


class ModelVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable snapshot of a model. Enhancements create new versions."""

    __tablename__ = "model_versions"

    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True
    )
    version_number: Mapped[int]
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL", use_alter=True)
    )
    source: Mapped[ModelVersionSource] = mapped_column(str_enum(ModelVersionSource))
    quality_mode: Mapped[QualityMode | None] = mapped_column(str_enum(QualityMode))
    provider_key: Mapped[str | None] = mapped_column(String(64))
    surface_provenance: Mapped[SurfaceProvenance] = mapped_column(
        str_enum(SurfaceProvenance), default=SurfaceProvenance.UNKNOWN
    )
    primary_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL", use_alter=True)
    )
    # vertex/triangle counts, bounding box, materials… (see quality reports for checks)
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")

    model: Mapped[Model3D] = relationship(back_populates="versions", foreign_keys=[model_id])
    parts: Mapped[list["ModelPart"]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (UniqueConstraint("model_id", "version_number"),)


class ModelPart(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A node/mesh in a version's scene hierarchy."""

    __tablename__ = "model_parts"

    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), index=True
    )
    parent_part_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_parts.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(255))
    node_path: Mapped[str] = mapped_column(String(1024))
    mesh_index: Mapped[int | None]
    material_name: Mapped[str | None] = mapped_column(String(255))
    vertex_count: Mapped[int] = mapped_column(default=0)
    triangle_count: Mapped[int] = mapped_column(default=0)
    bbox: Mapped[dict[str, Any] | None]
