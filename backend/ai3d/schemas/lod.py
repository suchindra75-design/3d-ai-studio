import uuid
from typing import Literal

from ai3d.schemas.common import Schema


class LodLevelRead(Schema):
    level: int
    triangles: int
    # Distance from the original vertices to their simplified counterparts, as a share
    # of the model's diagonal.
    mean_shift: float
    max_shift: float
    size_bytes: int
    file_url: str


class LodRead(Schema):
    version_id: uuid.UUID
    # not_needed: small enough to draw as it is; missing: not built yet; ready: levels
    # below; unavailable: building found no level that kept the shape.
    status: Literal["not_needed", "missing", "ready", "unavailable"]
    triangles: int
    levels: list[LodLevelRead]
    message: str | None = None
