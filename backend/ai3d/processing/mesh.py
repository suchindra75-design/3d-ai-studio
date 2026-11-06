"""Validation and quality report for 3D files.

The file is re-opened from disk with trimesh — the same bytes that will be
stored and downloaded — and checked for usable, finite geometry. Results are
facts about the file (counts, bounds, watertightness); none of them says the
shape matches the real object.
"""

import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import psutil
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
        raw = np.asarray(attributes["color"])
        if raw.ndim != 2 or len(raw) != len(geometry.vertices):
            attributes.pop("color")
            continue
        # glTF stores colours as floats or as normalised 8- or 16-bit integers
        # (Blender writes 16-bit); trimesh hands over the raw values.
        if np.issubdtype(raw.dtype, np.integer):
            colours = raw.astype(np.float64) / float(np.iinfo(raw.dtype).max)
        else:
            colours = raw.astype(np.float64)
            if colours.max(initial=0.0) > 1.0:  # floats holding byte values
                colours = colours / 255.0
        if colours.shape[1] == 3:
            colours = np.hstack([colours, np.ones((len(colours), 1))])
        material = getattr(visual, "material", None)
        # Any texture (base colour, normal, occlusion...) needs the UVs and the
        # material kept, so the colours stay a vertex attribute next to them.
        textures = [
            getattr(material, name, None)
            for name in (
                "baseColorTexture",
                "image",
                "normalTexture",
                "occlusionTexture",
                "emissiveTexture",
                "metallicRoughnessTexture",
            )
        ]
        has_texture = (
            any(t is not None for t in textures) and getattr(visual, "uv", None) is not None
        )
        if has_texture:
            # Bytes: trimesh writes this attribute as normalised bytes by truncating, which
            # turned float colours (0-1) into black.
            attributes["color"] = np.round(colours * 255.0).astype(np.uint8)
            continue
        base_factor = getattr(material, "baseColorFactor", None)
        factor = np.asarray([255] * 4 if base_factor is None else base_factor, float)
        rgba = np.clip(colours * (factor[:4] / 255.0), 0.0, 1.0)
        geometry.visual = trimesh.visual.ColorVisuals(
            geometry, vertex_colors=np.round(rgba * 255.0).astype(np.uint8)
        )
    return scene


_UNSIGNED_SHORT = 5123
_UNSIGNED_BYTE = 5121


def narrow_glb_colours(data: bytes) -> bytes | None:
    """Rewrites 16-bit COLOR_0 data in a GLB as 8-bit; None when there is none.

    glTF allows normalised 16-bit colours (Blender writes them), but for meshes
    without a material trimesh turns them into bytes by wrapping around (7666 →
    242) instead of scaling, so the colours came out as noise. Each 16-bit
    accessor is copied, scaled to 8 bits, to the end of the binary chunk.
    """
    if data[:4] != b"glTF" or len(data) < 20:
        return None
    json_length = int.from_bytes(data[12:16], "little")
    doc = json.loads(data[20 : 20 + json_length])
    bin_at = 20 + json_length
    if len(data) < bin_at + 8 or data[bin_at + 4 : bin_at + 8] != b"BIN\x00":
        return None
    size = int.from_bytes(data[bin_at : bin_at + 4], "little")
    binary = bytearray(data[bin_at + 8 : bin_at + 8 + size])
    accessors = {
        primitive["attributes"]["COLOR_0"]
        for mesh in doc.get("meshes", [])
        for primitive in mesh.get("primitives", [])
        if "COLOR_0" in primitive.get("attributes", {})
    }
    changed = False
    for index in sorted(accessors):
        accessor = doc["accessors"][index]
        if accessor.get("componentType") != _UNSIGNED_SHORT or "bufferView" not in accessor:
            continue
        components = {"VEC3": 3, "VEC4": 4}.get(accessor.get("type", ""))
        view = doc["bufferViews"][accessor["bufferView"]]
        if components is None or "sparse" in accessor or view.get("buffer", 0) != 0:
            continue
        if view.get("byteStride", components * 2) != components * 2:
            continue  # interleaved: left to trimesh
        start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
        count = int(accessor["count"])
        values = np.frombuffer(bytes(binary), np.uint16, count * components, start)
        narrowed = np.round(values / 257.0).astype(np.uint8)
        binary.extend(b"\x00" * (-len(binary) % 4))
        doc["bufferViews"].append(
            {"buffer": 0, "byteOffset": len(binary), "byteLength": int(narrowed.nbytes)}
        )
        binary.extend(narrowed.tobytes())
        accessor.update(
            bufferView=len(doc["bufferViews"]) - 1,
            byteOffset=0,
            componentType=_UNSIGNED_BYTE,
            normalized=True,
        )
        changed = True
    if not changed:
        return None
    binary.extend(b"\x00" * (-len(binary) % 4))
    doc["buffers"][0]["byteLength"] = len(binary)
    text = json.dumps(doc, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    total = 12 + 8 + len(text) + 8 + len(binary)
    return b"".join(
        [
            b"glTF",
            (2).to_bytes(4, "little"),
            total.to_bytes(4, "little"),
            len(text).to_bytes(4, "little"),
            b"JSON",
            text,
            len(binary).to_bytes(4, "little"),
            b"BIN\x00",
            bytes(binary),
        ]
    )


def load_model_scene(path: Path, *, file_type: str = "glb") -> trimesh.Scene:
    """Loads a model file for processing, with vertex colours normalised."""
    if file_type == "glb":
        narrowed = narrow_glb_colours(path.read_bytes())
        if narrowed is not None:
            stream = trimesh.util.wrap_as_stream(narrowed)
            scene = trimesh.load_scene(stream, file_type="glb", process=False)
            return normalise_vertex_colours(scene)
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


# Triangles handled at a time by the chunked measurements below (bounds temporary
# memory on models of millions of triangles).
_CHUNK = 1_000_000


def _degenerate_count(vertices: np.ndarray, faces: np.ndarray) -> int:
    """Triangles with (almost) no area, as trimesh's ``nondegenerate_faces`` decides
    (cross-product length below its default tolerance), without caching."""
    tolerance = trimesh.tol.merge
    count = 0
    for start in range(0, len(faces), _CHUNK):
        corners = vertices[faces[start : start + _CHUNK]]
        cross = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
        count += int((np.linalg.norm(cross, axis=1) <= tolerance).sum())
    return count


def _face_centres(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    centres = np.empty((len(faces), 3), dtype=np.float32)
    for start in range(0, len(faces), _CHUNK):
        centres[start : start + _CHUNK] = vertices[faces[start : start + _CHUNK]].mean(axis=1)
    return centres


def _signed_volume(vertices: np.ndarray, faces: np.ndarray) -> float:
    """Enclosed volume (negative when the surface faces inwards): the sum of the signed
    tetrahedra from the origin, a sixth of v0 · (v1 × v2) per triangle."""
    total = 0.0
    for start in range(0, len(faces), _CHUNK):
        corners = vertices[faces[start : start + _CHUNK]]
        total += float(np.einsum("ij,ij->", corners[:, 0], np.cross(corners[:, 1], corners[:, 2])))
    return total / 6.0


# Peak memory of the check below: measured 0.94 GB for 1 million triangles and 4.7 GB
# for 10 million (docs/BENCHMARKS.md), about 450 MB + 430 bytes per triangle; this
# estimate keeps a margin.
VALIDATION_BASE_BYTES = 500 * 1024 * 1024
VALIDATION_BYTES_PER_TRIANGLE = 500


def validation_memory_bytes(triangles: int) -> int:
    return VALIDATION_BASE_BYTES + triangles * VALIDATION_BYTES_PER_TRIANGLE


def glb_triangle_count(path: Path) -> int | None:
    """Triangles in a GLB, read from its JSON header only (no geometry is loaded).

    Counts triangle primitives (mode 4, the default) as indices / 3, or positions / 3
    when not indexed. None when the header cannot be read."""
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
            if len(header) < 20 or header[:4] != b"glTF":
                return None
            length, kind = struct.unpack("<II", header[12:20])
            if kind != 0x4E4F534A:  # "JSON"
                return None
            document = json.loads(handle.read(length))
        accessors = document.get("accessors", [])
        nodes_meshes = [n.get("mesh") for n in document.get("nodes", []) if "mesh" in n]
        total = 0
        for index in nodes_meshes:
            for primitive in document["meshes"][index].get("primitives", []):
                if primitive.get("mode", 4) != 4:
                    continue
                source = primitive.get("indices", primitive.get("attributes", {}).get("POSITION"))
                if source is not None:
                    total += int(accessors[source]["count"]) // 3
        return total
    except (OSError, ValueError, KeyError, IndexError, TypeError, struct.error):
        return None


def _not_enough_memory(path: Path, file_type: str) -> MeshReport | None:
    """A failed report when checking this GLB would need more memory than is free."""
    if file_type != "glb":
        return None
    triangles = glb_triangle_count(path)
    if not triangles:
        return None
    need = validation_memory_bytes(triangles)
    free = int(psutil.virtual_memory().available)
    if need <= free:
        return None
    return MeshReport(
        status=ReportStatus.FAIL,
        metrics={"triangle_count": triangles},
        issues=[
            MeshIssue(
                "not_enough_memory",
                "error",
                f"Checking this {triangles:,}-triangle model needs about "
                f"{need / 1024**3:.1f} GB of memory and {free / 1024**3:.1f} GB is free. "
                "Close other programs and try again.",
            )
        ],
    )


def validate_mesh_file(path: Path, *, file_type: str = "glb") -> MeshReport:
    short = _not_enough_memory(path, file_type)
    if short is not None:
        return short
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
    degenerate = _degenerate_count(vertices, faces)
    del combined  # its caches would stay alive otherwise
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
        centres = _face_centres(vertices, faces)
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
    volume = (
        _signed_volume(np.asarray(shape.vertices), np.asarray(shape.faces)) if watertight else 0.0
    )
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
        "volume": round(abs(volume), 9) if watertight else None,
        # A closed, consistently wound surface with negative volume faces inwards.
        "inside_out": bool(watertight and shape.is_winding_consistent and volume < 0)
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
