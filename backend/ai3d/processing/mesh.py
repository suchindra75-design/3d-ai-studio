"""Validation and quality report for 3D files.

The file is re-opened from disk with trimesh — the same bytes that will be
stored and downloaded — and checked for usable, finite geometry. Results are
facts about the file (counts, bounds, watertightness); none of them says the
shape matches the real object.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from ai3d.models.enums import ReportStatus

log = logging.getLogger(__name__)

# 2: adds edge topology (boundary / non-manifold), duplicate faces, winding,
#    UV range and texture sizes.
REPORT_VERSION = 2
# Component analysis builds a face-adjacency graph; skip it for huge meshes.
COMPONENT_ANALYSIS_MAX_FACES = 3_000_000
# More pieces than this, with none holding half the surface, is a fragmented surface.
FRAGMENTED_PIECES = 20
# A bounding box this many times larger than the central 95% of the surface (measured: a
# real sparse photogrammetry result 18×, generated and scanned models 1.1–1.7×).
STRAY_SPREAD = 4.0
HIGH_TRIANGLE_COUNT = 1_500_000


@dataclass(slots=True)
class MeshIssue:
    code: str
    severity: str  # "error" | "warning" | "info"
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass(slots=True)
class MeshReport:
    status: ReportStatus
    metrics: dict[str, Any] = field(default_factory=dict)
    issues: list[MeshIssue] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.status != ReportStatus.FAIL


def _materials(scene: trimesh.Scene) -> tuple[int, int, bool]:
    names: set[str] = set()
    textures = 0
    has_uv = False
    for geom in scene.geometry.values():
        visual = getattr(geom, "visual", None)
        if visual is None:
            continue
        if getattr(visual, "uv", None) is not None and len(visual.uv):
            has_uv = True
        material = getattr(visual, "material", None)
        if material is None:
            continue
        names.add(getattr(material, "name", None) or str(id(material)))
        for attr in (
            "baseColorTexture",
            "metallicRoughnessTexture",
            "normalTexture",
            "occlusionTexture",
            "emissiveTexture",
            "image",
        ):
            if getattr(material, attr, None) is not None:
                textures += 1
    return len(names), textures, has_uv


def _texture_sizes(scene: trimesh.Scene) -> list[list[int]]:
    sizes: list[list[int]] = []
    seen: set[int] = set()
    for geom in scene.geometry.values():
        material = getattr(getattr(geom, "visual", None), "material", None)
        for attr in (
            "baseColorTexture",
            "metallicRoughnessTexture",
            "normalTexture",
            "occlusionTexture",
            "emissiveTexture",
            "image",
        ):
            image = getattr(material, attr, None) if material is not None else None
            if image is not None and hasattr(image, "size") and id(image) not in seen:
                seen.add(id(image))
                sizes.append([int(image.size[0]), int(image.size[1])])
    return sizes


def _uv_out_of_range(meshes: list[trimesh.Trimesh]) -> float | None:
    uvs = [
        np.asarray(uv)
        for m in meshes
        if (uv := getattr(m.visual, "uv", None)) is not None and len(uv)
    ]
    if not uvs:
        return None
    all_uv = np.concatenate(uvs)
    outside = ((all_uv < -1e-3) | (all_uv > 1 + 1e-3)).any(axis=1)
    return round(float(outside.mean()), 4)


def _shape_findings(metrics: dict[str, Any]) -> list[MeshIssue]:
    """Whether the surface is one compact shape, or scattered / shattered."""
    issues: list[MeshIssue] = []
    components = metrics["components"]
    spread = metrics.get("spread_ratio")
    if spread is not None and spread > STRAY_SPREAD:
        issues.append(
            MeshIssue(
                "stray_surface",
                "warning",
                f"Stray triangles reach far from the main surface: the model's box is "
                f"{spread:.0f}× larger than the region holding 95% of it, so its size and "
                "framing are misleading and the shape is probably not usable as it is. "
                "Photogrammetry without a dense step (no NVIDIA GPU) often looks like this "
                "on real photos.",
            )
        )
    share = metrics.get("largest_component_share")
    if components is not None and components > FRAGMENTED_PIECES and (share or 0) < 0.5:
        issues.append(
            MeshIssue(
                "fragmented",
                "warning",
                f"The surface is broken into {components:,} pieces and the largest holds only "
                f"{(share or 0):.0%} of it, so the shape is probably not usable as it is. "
                "Sparse photogrammetry (no dense step) on real photos often looks like this.",
            )
        )
    elif components is not None and components > 1:
        issues.append(
            MeshIssue(
                "multiple_components",
                "info",
                f"The model consists of {components} separate pieces.",
            )
        )
    return issues


def _findings(metrics: dict[str, Any]) -> list[MeshIssue]:
    issues: list[MeshIssue] = []
    degenerate = metrics["degenerate_triangles"]
    triangles = metrics["triangle_count"]
    if max(metrics["bbox_size"]) <= 0:
        issues.append(MeshIssue("flat", "error", "The model has zero size."))
    if degenerate:
        issues.append(
            MeshIssue("degenerate_triangles", "warning", f"{degenerate} triangles have zero area.")
        )
    if not metrics["watertight"]:
        issues.append(
            MeshIssue(
                "not_watertight",
                "info",
                "The surface has holes or open edges; 3D printing needs repair first.",
            )
        )
    issues.extend(_shape_findings(metrics))
    if triangles > HIGH_TRIANGLE_COUNT:
        issues.append(
            MeshIssue(
                "high_triangle_count",
                "warning",
                f"{triangles:,} triangles may be slow in viewers and games.",
            )
        )
    if not metrics["texture_count"]:
        issues.append(MeshIssue("no_textures", "info", "The model has no textures."))
    non_manifold = metrics.get("non_manifold_edges") or 0
    if non_manifold:
        issues.append(
            MeshIssue(
                "non_manifold",
                "warning",
                f"{non_manifold:,} edges are shared by more than two triangles (non-manifold); "
                "many 3D-printing and CAD tools reject this.",
            )
        )
    if metrics.get("winding_consistent") is False:
        issues.append(
            MeshIssue(
                "inconsistent_winding",
                "warning",
                "Some triangles face the opposite way to their neighbours (inconsistent "
                "normals); Enhance can fix this.",
            )
        )
    if metrics.get("inside_out"):
        issues.append(
            MeshIssue(
                "inside_out",
                "warning",
                "The model is inside out: its triangles face inwards, so lighting looks wrong "
                "and 3D-printing tools may reject it. Enhance (High or Professional) fixes this.",
            )
        )
    duplicates = metrics.get("duplicate_faces") or 0
    if duplicates:
        issues.append(
            MeshIssue("duplicate_faces", "warning", f"{duplicates:,} triangles are duplicated.")
        )
    uv_outside = metrics.get("uv_out_of_range")
    if uv_outside:
        issues.append(
            MeshIssue(
                "uv_out_of_range",
                "info",
                f"{uv_outside:.0%} of texture coordinates lie outside 0–1 (the texture repeats "
                "there).",
            )
        )
    if any(max(size) > 8192 for size in metrics.get("texture_sizes") or []):
        issues.append(
            MeshIssue(
                "large_texture",
                "warning",
                "A texture is larger than 8192 px; some viewers and phones cannot load it.",
            )
        )
    return issues


def normalise_vertex_colours(scene: trimesh.Scene) -> trimesh.Scene:
    """Makes glTF COLOR_0 usable everywhere, in place.

    When a primitive has a material *and* COLOR_0, trimesh keeps the colours as a
    float RGB vertex attribute next to the material. Its own glTF writer then
    fails ("cannot reshape … into shape (4)"), and nothing sees the colours.
    Material-only meshes become plain per-vertex colours (the base-colour factor
    multiplied in, as glTF does); textured meshes keep their texture and get the
    colours as RGBA.
    """
    for geometry in scene.geometry.values():
        visual = getattr(geometry, "visual", None)
        attributes = getattr(visual, "vertex_attributes", None)
        if not isinstance(geometry, trimesh.Trimesh) or visual is None or visual.kind != "texture":
            continue
        if not attributes or "color" not in attributes:
            continue
        colours = np.asarray(attributes["color"], dtype=np.float64)
        if colours.ndim != 2 or len(colours) != len(geometry.vertices):
            attributes.pop("color")
            continue
        if colours.max(initial=0.0) > 1.0:  # stored as bytes
            colours = colours / 255.0
        if colours.shape[1] == 3:
            colours = np.hstack([colours, np.ones((len(colours), 1))])
        material = getattr(visual, "material", None)
        image = getattr(material, "baseColorTexture", None) or getattr(material, "image", None)
        has_texture = image is not None and getattr(visual, "uv", None) is not None
        if has_texture:
            attributes["color"] = colours.astype(np.float32)
            continue
        factor = np.asarray(getattr(material, "baseColorFactor", None) or [255] * 4, float)
        rgba = np.clip(colours * (factor[:4] / 255.0), 0.0, 1.0)
        geometry.visual = trimesh.visual.ColorVisuals(
            geometry, vertex_colors=np.round(rgba * 255.0).astype(np.uint8)
        )
    return scene


def load_model_scene(path: Path, *, file_type: str = "glb") -> trimesh.Scene:
    """Loads a model file for processing, with vertex colours normalised."""
    return normalise_vertex_colours(trimesh.load_scene(path, file_type=file_type, process=False))


def row_keys(rows: np.ndarray, base: int) -> np.ndarray:
    """One int64 per row of 2 or 3 vertex indices (each < ``base``), for fast 1-D sorting.

    ``np.unique(..., axis=0)`` sorts rows as structured data, which took 38 s on a
    6-million-triangle model; sorting one integer key per row takes about a second.
    Three indices need ``base**3 < 2**63`` (about 2 million vertices); beyond that the
    rows are ranked pairwise first.
    """
    rows = np.asarray(rows, dtype=np.int64)
    if rows.shape[1] == 2 or base**3 < 2**62:
        key = np.zeros(len(rows), dtype=np.int64)
        for column in range(rows.shape[1]):
            key = key * base + rows[:, column]
        return key
    _, pair = np.unique(rows[:, 0] * base + rows[:, 1], return_inverse=True)
    keys: np.ndarray = np.asarray(pair, dtype=np.int64) * base + rows[:, 2]
    return keys


def duplicate_face_count(faces: np.ndarray, vertex_count: int) -> tuple[int, np.ndarray]:
    """Triangles using the same three vertices as an earlier one, and the first of each."""
    _, first = np.unique(row_keys(np.sort(faces, axis=1), max(vertex_count, 1)), return_index=True)
    return len(faces) - len(first), first


def validate_mesh_file(path: Path, *, file_type: str = "glb") -> MeshReport:
    try:
        scene = load_model_scene(path, file_type=file_type)
    except Exception as exc:  # trimesh raises many exception types for bad input
        log.info("mesh could not be loaded", extra={"error": type(exc).__name__})
        return MeshReport(
            status=ReportStatus.FAIL,
            issues=[MeshIssue("unreadable", "error", "The file is not a readable 3D model.")],
        )

    meshes = [g for g in scene.geometry.values() if isinstance(g, trimesh.Trimesh)]
    issues: list[MeshIssue] = []
    if not meshes or sum(len(m.faces) for m in meshes) == 0:
        return MeshReport(
            status=ReportStatus.FAIL,
            metrics={"mesh_count": len(meshes)},
            issues=[MeshIssue("no_geometry", "error", "The file contains no triangles.")],
        )

    combined = scene.to_mesh()
    vertices = np.asarray(combined.vertices)
    faces = np.asarray(combined.faces)
    finite = bool(np.isfinite(vertices).all())
    if not finite:
        return MeshReport(
            status=ReportStatus.FAIL,
            metrics={"vertex_count": len(vertices), "triangle_count": len(faces)},
            issues=[MeshIssue("non_finite", "error", "Vertex positions contain NaN or infinity.")],
        )

    bounds = combined.bounds
    size = bounds[1] - bounds[0]
    degenerate = int((~combined.nondegenerate_faces()).sum())
    material_count, texture_count, has_uv = _materials(scene)

    # Texture seams and normal splits duplicate vertices, which would make every UV
    # island look like a separate, open piece. Topology is judged on the shape alone.
    shape = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
    shape.merge_vertices(merge_tex=True, merge_norm=True)

    components: int | None = None
    largest_share: float | None = None
    if len(faces) <= COMPONENT_ANALYSIS_MAX_FACES:
        labels = trimesh.graph.connected_component_labels(  # type: ignore[no-untyped-call]
            shape.face_adjacency, node_count=len(shape.faces)
        )
        components = int(labels.max()) + 1 if len(labels) else 0
        if len(labels):
            largest_share = round(float(np.bincount(labels).max()) / len(labels), 4)

    # How far the bounding box reaches beyond where most of the surface is: a few stray
    # triangles (e.g. sparse photogrammetry reaching background points) inflate it.
    spread: float | None = None
    if len(faces):
        centres = vertices[faces].mean(axis=1)
        core = np.percentile(centres, 97.5, axis=0) - np.percentile(centres, 2.5, axis=0)
        core_diagonal = float(np.linalg.norm(core))
        if core_diagonal > 0:
            spread = round(float(np.linalg.norm(size)) / core_diagonal, 2)

    watertight = bool(shape.is_watertight)
    # How many triangles use each edge: 1 = open boundary, 3+ = non-manifold.
    _, edge_counts = np.unique(
        row_keys(shape.edges_sorted, max(len(shape.vertices), 1)), return_counts=True
    )
    boundary_edges = int((edge_counts == 1).sum())
    non_manifold_edges = int((edge_counts > 2).sum())
    duplicate_faces, _ = duplicate_face_count(shape.faces, len(shape.vertices))
    metrics: dict[str, Any] = {
        "mesh_count": len(meshes),
        "vertex_count": len(vertices),
        "triangle_count": len(faces),
        "material_count": material_count,
        "texture_count": texture_count,
        "has_uv": has_uv,
        "bbox_min": [round(float(v), 6) for v in bounds[0]],
        "bbox_max": [round(float(v), 6) for v in bounds[1]],
        "bbox_size": [round(float(v), 6) for v in size],
        "watertight": watertight,
        "components": components,
        # Share of the triangles in the biggest piece (1.0 = all in one piece).
        "largest_component_share": largest_share,
        # Bounding-box diagonal / diagonal of the central 95% of the surface (≈1 is compact).
        "spread_ratio": spread,
        "degenerate_triangles": degenerate,
        "file_bytes": path.stat().st_size,
        "boundary_edges": boundary_edges,
        "non_manifold_edges": non_manifold_edges,
        "duplicate_faces": duplicate_faces,
        "winding_consistent": bool(shape.is_winding_consistent)
        if non_manifold_edges == 0
        else None,
        "volume": round(abs(float(shape.volume)), 9) if watertight else None,
        # A closed, consistently wound surface with negative volume faces inwards.
        "inside_out": bool(watertight and shape.is_winding_consistent and shape.volume < 0)
        if watertight and non_manifold_edges == 0
        else None,
        "uv_out_of_range": _uv_out_of_range(meshes),
        "texture_sizes": _texture_sizes(scene),
    }

    issues.extend(_findings(metrics))
    if any(i.severity == "error" for i in issues):
        status = ReportStatus.FAIL
    elif any(i.severity == "warning" for i in issues):
        status = ReportStatus.WARN
    else:
        status = ReportStatus.PASS
    return MeshReport(status=status, metrics=metrics, issues=issues)
