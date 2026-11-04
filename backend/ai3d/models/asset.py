import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ai3d.database.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin, str_enum
from ai3d.models.enums import AssetKind


class Asset(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A stored file (mesh, texture, point cloud, export…). Bytes live in storage."""

    __tablename__ = "assets"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[AssetKind] = mapped_column(str_enum(AssetKind))
    storage_backend: Mapped[str] = mapped_column(String(32), default="local")
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    # ``metadata`` is reserved by SQLAlchemy's declarative API.
    meta: Mapped[dict[str, Any]] = mapped_column(default=dict)
