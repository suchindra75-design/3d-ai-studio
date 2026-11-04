from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from ai3d.database.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin


class AppSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """User-editable application settings (non-secret), one JSON value per key."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), unique=True)
    value: Mapped[Any] = mapped_column(JSONType, nullable=True)
