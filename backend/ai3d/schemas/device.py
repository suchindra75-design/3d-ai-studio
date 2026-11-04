import uuid
from datetime import datetime
from typing import Any

from ai3d.schemas.common import APIModel, Schema


class CapabilityRead(Schema):
    supported: bool
    reason: str


class LocalDeviceRead(APIModel):
    id: uuid.UUID
    machine_id: str
    hostname: str
    os_name: str
    os_version: str
    arch: str
    cpu_model: str
    cpu_cores_physical: int | None
    cpu_cores_logical: int | None
    ram_total_bytes: int | None
    disk_free_bytes: int | None
    gpus: list[dict[str, Any]]
    accelerators: dict[str, Any]
    dependencies: dict[str, Any]
    capabilities: dict[str, CapabilityRead]
    last_seen_at: datetime
