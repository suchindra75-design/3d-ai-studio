import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ai3d.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, str_enum
from ai3d.models.enums import ImageKind, QualityLevel

if TYPE_CHECKING:
    from ai3d.models.project import Project


class Image(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A source photo or an image derived from one. Bytes live in storage."""

    __tablename__ = "images"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[ImageKind] = mapped_column(str_enum(ImageKind), default=ImageKind.ORIGINAL)
    derived_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(default=0)

    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    thumbnail_key: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))

    # Dimensions as displayed (after EXIF orientation is applied).
    width: Mapped[int]
    height: Mapped[int]
    exif_orientation: Mapped[int | None]

    camera_make: Mapped[str | None] = mapped_column(String(128))
    camera_model: Mapped[str | None] = mapped_column(String(128))
    focal_length_mm: Mapped[float | None]
    focal_length_35mm: Mapped[float | None]
    # Curated EXIF subset (GPS is deliberately excluded).
    exif: Mapped[dict[str, Any]] = mapped_column(default=dict)

    quality_level: Mapped[QualityLevel] = mapped_column(str_enum(QualityLevel))
    sharpness: Mapped[float | None]
    analysis: Mapped[dict[str, Any]] = mapped_column(default=dict)

    project: Mapped["Project"] = relationship(back_populates="images")

    __table_args__ = (
        # Re-uploading the same bytes into a project is idempotent.
        UniqueConstraint("project_id", "sha256", "kind"),
        Index("ix_images_project_id_position", "project_id", "position"),
    )
