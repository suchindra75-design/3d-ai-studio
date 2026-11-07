"""Model health inspection (D69) and repairs (D70)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from ai3d.schemas.common import Schema

Category = Literal["integrity", "topology", "surface", "uv_texture", "fabrication"]
RepairCode = Literal[
    "recalculate_normals",
    "fix_winding",
    "remove_degenerate_faces",
    "remove_duplicate_faces",
    "merge_duplicate_vertices",
    "fill_small_holes",
    "separate_components",
    "remove_components",
    "repair_non_manifold",
]


class InspectionRequest(Schema):
    printability: bool = Field(
        default=False, description="Also run the 3D-printing checks (wall thickness, overhangs…)."
    )
    min_wall_mm: float = Field(
        default=1.0, gt=0, le=100, description="Thinnest wall or feature to accept, in mm."
    )
    overhang_degrees: float = Field(
        default=45, ge=10, le=80, description="Steepest overhang that prints without supports."
    )
    self_intersections: Literal["auto", "always", "never"] = Field(
        default="auto",
        description="auto: only up to 2 million triangles (above, the search takes minutes).",
    )


class LocationsRead(Schema):
    """Where a finding is, in the model's world space (glTF axes), at most 2,000."""

    points: list[list[float]]
    segments: list[list[float]]
    triangles: list[list[float]]
    shown: int
    total: int


class FindingRead(Schema):
    code: str
    category: Category
    severity: Literal["error", "warning", "info"]
    title: str
    count: float
    unit: str
    explanation: str
    method: Literal["exact", "approximate", "sampled"]
    repair: RepairCode | None
    repair_risk: str | None
    meshes: list[str]
    locations: LocationsRead
    details: dict[str, Any]


class SkippedRead(Schema):
    code: str
    category: Category
    title: str
    reason: str


class InspectionReportRead(Schema):
    report_version: int
    triangles: int
    vertices: int
    meshes: int
    findings: list[FindingRead]
    skipped: list[SkippedRead]
    measurements: dict[str, dict[str, Any]]
    options: dict[str, Any]
    seconds: float


class InspectionRead(Schema):
    version_id: uuid.UUID
    status: Literal["missing", "running", "ready", "failed"]
    message: str | None = None
    report: InspectionReportRead | None = None
    created_at: datetime | None = None
    json_url: str | None = None
    markdown_url: str | None = None


class RepairOperationIn(Schema):
    code: RepairCode
    pieces: list[int] = Field(
        default_factory=list,
        max_length=10_000,
        description="remove_components: the piece ids (from the inspection report) to delete.",
    )

    @model_validator(mode="after")
    def _pieces_only_when_removing(self) -> "RepairOperationIn":
        if self.code == "remove_components" and not self.pieces:
            raise ValueError("remove_components needs the pieces to remove.")
        if self.code != "remove_components" and self.pieces:
            raise ValueError("Only remove_components takes pieces.")
        return self


class RepairRequest(Schema):
    # Omitted = the model's current version. The source version is never changed.
    source_version_id: uuid.UUID | None = None
    operations: list[RepairOperationIn] = Field(min_length=1, max_length=9)
