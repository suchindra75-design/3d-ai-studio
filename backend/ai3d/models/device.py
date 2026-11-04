from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from ai3d.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LocalDevice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A machine that can run local processing, as last detected."""

    __tablename__ = "local_devices"

    machine_id: Mapped[str] = mapped_column(String(128), unique=True)
    hostname: Mapped[str] = mapped_column(String(255))
    os_name: Mapped[str] = mapped_column(String(64))
    os_version: Mapped[str] = mapped_column(String(128))
    arch: Mapped[str] = mapped_column(String(32))
    cpu_model: Mapped[str] = mapped_column(String(255))
    cpu_cores_physical: Mapped[int | None]
    cpu_cores_logical: Mapped[int | None]
    ram_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpus: Mapped[list[Any]] = mapped_column(default=list)
    accelerators: Mapped[dict[str, Any]] = mapped_column(default=dict)
    dependencies: Mapped[dict[str, Any]] = mapped_column(default=dict)
    capabilities: Mapped[dict[str, Any]] = mapped_column(default=dict)
    last_seen_at: Mapped[datetime]
