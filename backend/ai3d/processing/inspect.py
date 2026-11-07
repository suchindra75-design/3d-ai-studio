"""Model health inspection (D69): find what is wrong with a model, where, and what
could be done about it.

Everything runs on the model as stored (glTF world space, metres or model units),
with positions welded within 10⁻⁷ of the model's size, so texture seams and
normal splits (vertices duplicated on purpose) are not mistaken for holes. Each
finding says how it was measured:

- ``exact``: computed on every triangle or edge (exact for the stored
  floating-point geometry and the welding tolerance);
- ``approximate``: a geometric rule that can miss or over-report special cases
  (each finding says which);
- ``sampled``: measured at sample points (results depend on the sample).

Locations are capped (``MAX_LOCATIONS``); counts never are. Nothing here changes
the model.
"""

from __future__ import annotations

import io
import json
import math
import struct
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

REPORT_VERSION = 1
# Locations kept per finding (counts are always complete).
MAX_LOCATIONS = 2000
# Self-intersections are looked for on models up to this size unless asked for:
# above it the pair tests take minutes (docs/BENCHMARKS.md).
SELF_INTERSECTION_MAX_FACES = 2_000_000
# Wall thickness: rays from this many surface points (area-weighted, fixed seed).
THICKNESS_SAMPLES = 2000
# UV overlaps are measured on meshes up to this size (above, the island search needs
# more memory than the rest of the inspection); larger ones are reported as skipped.
UV_OVERLAP_MAX_FACES = 5_000_000
# A density note above this many triangles (levels of detail are used to draw it).
HEAVY_TRIANGLES = 1_500_000

CATEGORIES = {
    "integrity": "Geometry integrity",
    "topology": "Topology",
    "surface": "Surface quality",
    "uv_texture": "UV and texture integrity",
    "fabrication": "Fabrication suitability",
}
_SEVERITY = {"error": 0, "warning": 1, "info": 2}
_CHUNK = 1_000_000
# A ray direction along no axis (internal-shell test).
_OBLIQUE = np.array([0.577, 0.578, 0.5765]) / np.linalg.norm([0.577, 0.578, 0.5765])

Add = Callable[..., None]


@dataclass(slots=True)
class Locations:
    """Where a finding is, in the model's world space (glTF axes)."""

    points: list[list[float]] = field(default_factory=list)
    segments: list[list[float]] = field(default_factory=list)  # x0 y0 z0 x1 y1 z1
    triangles: list[list[float]] = field(default_factory=list)  # three corners
    shown: int = 0  # how many elements the lists above hold
    total: int = 0  # how many there are


@dataclass(slots=True)
class Finding:
    code: str
    category: str
    severity: str  # error | warning | info
    title: str
    count: float
    unit: str
    explanation: str
    method: str  # exact | approximate | sampled
    repair: str | None = None  # a repair operation that addresses it
    repair_risk: str | None = None
    meshes: list[str] = field(default_factory=list)
    locations: Locations = field(default_factory=Locations)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Skipped:
    code: str
    category: str
    title: str
    reason: str


@dataclass(slots=True)
class Options:
    printability: bool = False
    # Minimum wall thickness and feature size for printing, in millimetres (needs a
    # real-world scale; otherwise compared in model units with the same number).
    min_wall_mm: float = 1.0
    overhang_degrees: float = 45.0
    # "auto": up to SELF_INTERSECTION_MAX_FACES; "always"; "never".
    self_intersections: str = "auto"
    # Model units per millimetre when the model has a real-world scale (glTF metres:
    # 0.001); None when its size is unknown.
    units_per_mm: float | None = None


@dataclass(slots=True)
class Report:
    report_version: int
    triangles: int
    vertices: int
    meshes: int
    findings: list[Finding]
    skipped: list[Skipped]
    measurements: dict[str, dict[str, Any]]
    options: dict[str, Any]
    seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sort(findings: list[Finding]) -> None:
    findings.sort(key=lambda f: (list(CATEGORIES).index(f.category), _SEVERITY[f.severity]))


# -- geometry gathered once ------------------------------------------------------------


@dataclass(slots=True)
class _Model:
    vertices: np.ndarray  # world space, all meshes concatenated
    faces: np.ndarray  # into ``vertices``
    welded: np.ndarray  # welded vertex id of each vertex
    welded_faces: np.ndarray
    welded_count: int
    mesh_of_face: np.ndarray  # index into ``names``
    names: list[str]
    meshes: list[trimesh.Trimesh]
    diagonal: float


def _stored_normals(mesh: trimesh.Trimesh) -> np.ndarray | None:
    """Vertex normals read from the file, or None (never computed here)."""
    stored = mesh._cache.cache.get("vertex_normals")  # what the file held
    if stored is None or len(stored) != len(mesh.vertices):
        return None
    return np.asarray(stored, dtype=np.float64)


def _empty_model() -> _Model:
    return _Model(
        vertices=np.zeros((0, 3)),
        faces=np.zeros((0, 3), np.int64),
        welded=np.zeros(0, np.int64),
        welded_faces=np.zeros((0, 3), np.int64),
        welded_count=0,
        mesh_of_face=np.zeros(0, np.int32),
        names=[],
        meshes=[],
        diagonal=1.0,
    )


def _gather(scene: trimesh.Scene) -> _Model:
    vertices: list[np.ndarray] = []
    faces: list[np.ndarray] = []
    owner: list[np.ndarray] = []
    names: list[str] = []
    meshes: list[trimesh.Trimesh] = []
    offset = 0
    for node in scene.graph.nodes_geometry:
        transform, geometry_name = scene.graph[node]
        mesh = scene.geometry.get(geometry_name)
        if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
            continue
        world = trimesh.transform_points(np.asarray(mesh.vertices, dtype=np.float64), transform)
        vertices.append(world)
        faces.append(np.asarray(mesh.faces, dtype=np.int64) + offset)
        owner.append(np.full(len(mesh.faces), len(names), dtype=np.int32))
        names.append(str(geometry_name))
        meshes.append(mesh)
        offset += len(world)
    if not faces:
        return _empty_model()
    all_vertices = np.concatenate(vertices)
    all_faces = np.concatenate(faces)
    finite = np.isfinite(all_vertices).all(axis=1)
    low = all_vertices[finite].min(axis=0) if finite.any() else np.zeros(3)
    high = all_vertices[finite].max(axis=0) if finite.any() else np.ones(3)
    diagonal = float(np.linalg.norm(high - low)) or 1.0
    # Weld positions within 1e-7 of the model's size (seams, normal splits).
    quantised = np.round(np.nan_to_num(all_vertices) / (diagonal * 1e-7)).astype(np.int64)
    _, welded = trimesh.grouping.unique_rows(quantised)  # type: ignore[no-untyped-call]
    welded = np.asarray(welded, dtype=np.int64)
    return _Model(
        vertices=all_vertices,
        faces=all_faces,
        welded=welded,
        welded_faces=welded[all_faces],
        welded_count=int(welded.max()) + 1 if len(welded) else 0,
        mesh_of_face=np.concatenate(owner),
        names=names,
        meshes=meshes,
        diagonal=diagonal,
    )


def _round(values: np.ndarray) -> list[list[float]]:
    rounded: list[list[float]] = np.round(np.asarray(values, dtype=np.float64), 6).tolist()
    return rounded


def _pick(indices: np.ndarray) -> np.ndarray:
    """At most MAX_LOCATIONS of ``indices``, spread evenly (deterministic)."""
    if len(indices) <= MAX_LOCATIONS:
        return indices
    return indices[np.linspace(0, len(indices) - 1, MAX_LOCATIONS).astype(np.int64)]


def _triangle_locations(model: _Model, faces: np.ndarray) -> Locations:
    picked = _pick(np.asarray(faces, dtype=np.int64))
    corners = model.vertices[model.faces[picked]].reshape(-1, 9)
    return Locations(triangles=_round(corners), shown=len(picked), total=len(faces))


def _segment_locations(model: _Model, edges: np.ndarray) -> Locations:
    """``edges``: (n, 2) vertex indices into ``model.vertices``."""
    picked = _pick(np.arange(len(edges)))
    ends = model.vertices[edges[picked]].reshape(-1, 6)
    return Locations(segments=_round(ends), shown=len(picked), total=len(edges))


def _point_locations(points: np.ndarray, total: int | None = None) -> Locations:
    picked = _pick(np.arange(len(points)))
    return Locations(
        points=_round(points[picked]),
        shown=len(picked),
        total=len(points) if total is None else total,
    )


def _meshes_of(model: _Model, faces: np.ndarray) -> list[str]:
    if not len(faces):
        return []
    return [model.names[i] for i in np.unique(model.mesh_of_face[faces])[:20]]


def _centres(model: _Model, faces: np.ndarray) -> np.ndarray:
    centres: np.ndarray = model.vertices[model.faces[faces]].mean(axis=1)
    return centres


# -- edges, holes, components ----------------------------------------------------------


@dataclass(slots=True)
class _Edges:
    boundary: np.ndarray  # directed edge ids (face * 3 + k) used by one triangle
    non_manifold: np.ndarray  # one directed edge id per edge used by 3+ triangles
    inconsistent: np.ndarray  # one directed edge id per edge whose 2 triangles agree
    unique_pairs: np.ndarray  # (m, 2) welded vertex ids of every distinct edge


def _edge_analysis(model: _Model) -> _Edges:
    # Directed edge face * 3 + k runs from corner k to corner k + 1; its key names the
    # undirected edge. Built a corner at a time, without full-size copies of the pairs.
    base = max(model.welded_count, 1)
    keys = np.empty(3 * len(model.welded_faces), np.int64)
    forward = np.empty(3 * len(model.welded_faces), bool)
    for k in range(3):
        a = model.welded_faces[:, k].astype(np.int64)
        b = model.welded_faces[:, (k + 1) % 3].astype(np.int64)
        keys[k::3] = np.minimum(a, b) * base + np.maximum(a, b)
        forward[k::3] = a < b
    del a, b
    order = np.argsort(keys, kind="stable")
    sorted_keys = keys[order]
    del keys
    starts = np.flatnonzero(np.r_[True, sorted_keys[1:] != sorted_keys[:-1]])
    counts = np.diff(np.r_[starts, len(sorted_keys)])
    unique_keys = sorted_keys[starts]
    del sorted_keys
    pairs = starts[counts == 2]
    same_direction = forward[order[pairs]] == forward[order[pairs + 1]]
    return _Edges(
        boundary=order[starts[counts == 1]],
        non_manifold=order[starts[counts > 2]],
        inconsistent=order[pairs[same_direction]],
        unique_pairs=np.stack([unique_keys // base, unique_keys % base], axis=1),
    )


def _edge_vertices(model: _Model, directed_ids: np.ndarray) -> np.ndarray:
    """(n, 2) indices into ``model.vertices`` of directed edges given by id."""
    face, k = directed_ids // 3, directed_ids % 3
    return np.stack([model.faces[face, k], model.faces[face, (k + 1) % 3]], axis=1)


def _connected(pairs: np.ndarray, count: int) -> np.ndarray:
    from scipy.sparse import coo_matrix  # noqa: PLC0415 - only needed here
    from scipy.sparse.csgraph import connected_components  # noqa: PLC0415

    graph = coo_matrix(
        (np.ones(len(pairs), dtype=np.int8), (pairs[:, 0], pairs[:, 1])), shape=(count, count)
    )
    _, labels = connected_components(graph, directed=False)
    result: np.ndarray = np.asarray(labels)
    return result


def _hole_loops(model: _Model, boundary_edges: np.ndarray) -> list[dict[str, Any]]:
    """Each connected set of open edges: a hole (or an open border), with its size."""
    ends = _edge_vertices(model, boundary_edges)
    used, local = np.unique(model.welded[ends], return_inverse=True)
    local = local.reshape(-1, 2)
    edge_label = _connected(local, len(used))[local[:, 0]]
    lengths = np.linalg.norm(model.vertices[ends[:, 0]] - model.vertices[ends[:, 1]], axis=1)
    centres = model.vertices[ends].mean(axis=1)
    loops: list[dict[str, Any]] = []
    for label in range(int(edge_label.max()) + 1):
        mask = edge_label == label
        loops.append(
            {
                "edges": int(mask.sum()),
                "perimeter": float(lengths[mask].sum()),
                "centre": _round(centres[mask].mean(axis=0, keepdims=True))[0],
            }
        )
    loops.sort(key=lambda loop: loop["edges"])
    return loops


def _areas_normals(model: _Model) -> tuple[np.ndarray, np.ndarray]:
    areas = np.empty(len(model.faces))
    normals = np.empty((len(model.faces), 3))
    for start in range(0, len(model.faces), _CHUNK):
        corners = model.vertices[model.faces[start : start + _CHUNK]]
        cross = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
        length = np.linalg.norm(cross, axis=1)
        areas[start : start + _CHUNK] = length / 2
        normals[start : start + _CHUNK] = cross / np.maximum(length, 1e-300)[:, None]
    return areas, normals


# -- a uniform grid for pair and ray queries --------------------------------------------


class _Grid:
    """Triangles registered in every cell their bounding box touches."""

    def __init__(self, model: _Model) -> None:
        self.model = model
        faces = np.arange(len(model.faces), dtype=np.int32)  # ids fit: half the memory
        low = np.full((len(faces), 3), np.inf)
        high = np.full((len(faces), 3), -np.inf)
        for k in range(3):
            corner = model.vertices[model.faces[:, k]]
            low, high = np.minimum(low, corner), np.maximum(high, corner)
        extent = np.max(high - low, axis=1)
        positive = extent[extent > 0]
        self.cell = float(np.median(positive) * 1.5) if len(positive) else model.diagonal
        self.origin = low.min(axis=0)
        first = np.floor((low - self.origin) / self.cell).astype(np.int64)
        last = np.floor((high - self.origin) / self.cell).astype(np.int64)
        self.dims = np.maximum(last.max(axis=0) + 1, 1)
        span = last - first + 1
        widest = span.max(axis=1)
        keys: list[np.ndarray] = []
        owners: list[np.ndarray] = []
        # Up to 4 cells per axis (99.9% of triangles on the scans measured): every cell
        # of the bounding box, vectorised.
        for size in (1, 2, 3, 4):
            group = np.flatnonzero(widest == size) if size > 1 else np.flatnonzero(widest <= 1)
            for offset in (
                np.array(np.meshgrid(*[np.arange(size)] * 3, indexing="ij")).reshape(3, -1).T
            ):
                inside = group[np.all(offset < span[group], axis=1)]
                keys.append(self._key(first[inside] + offset))
                owners.append(faces[inside])
        # Larger ones one by one: every cell of the box when that is at most 4,096 cells;
        # otherwise (long slivers) the cells of points sampled over the triangle a
        # quarter of a cell apart, which can miss a cell the triangle only grazes.
        self.sampled = 0
        for i in np.flatnonzero(widest > 4):
            if int(np.prod(span[i])) <= 4096:
                ranges = [np.arange(first[i, a], last[i, a] + 1) for a in range(3)]
                cells = np.stack(np.meshgrid(*ranges, indexing="ij"), axis=-1).reshape(-1, 3)
                cell_keys = self._key(cells)
            else:
                cell_keys = np.unique(
                    self._key(self._sampled_cells(model.vertices[model.faces[i]]))
                )
                self.sampled += 1
            keys.append(cell_keys)
            owners.append(np.full(len(cell_keys), faces[i]))
        self.incomplete = self.sampled > 0
        all_keys = np.concatenate(keys)
        order = np.argsort(all_keys, kind="stable")
        self.keys = all_keys[order]
        self.owners = np.concatenate(owners)[order]

    def _sampled_cells(self, corners: np.ndarray) -> np.ndarray:
        longest = float(np.max(np.linalg.norm(corners - np.roll(corners, 1, axis=0), axis=1)))
        steps = max(int(np.ceil(longest / (self.cell / 4))), 1)
        a, b = np.meshgrid(np.arange(steps + 1), np.arange(steps + 1), indexing="ij")
        keep = a + b <= steps
        u, v = a[keep] / steps, b[keep] / steps
        points = (
            corners[0] + np.outer(u, corners[1] - corners[0]) + np.outer(v, corners[2] - corners[0])
        )
        cells = np.floor((points - self.origin) / self.cell).astype(np.int64)
        clipped: np.ndarray = np.clip(cells, 0, self.dims - 1)
        return clipped

    def _key(self, cells: np.ndarray) -> np.ndarray:
        key: np.ndarray = (cells[:, 0] * self.dims[1] + cells[:, 1]) * self.dims[2] + cells[:, 2]
        return key


def _segment_hits(a0: np.ndarray, a1: np.ndarray, triangles: np.ndarray, eps: float) -> np.ndarray:
    """Whether each segment a0→a1 crosses its triangle (``triangles``: (n, 3, 3);
    Möller–Trumbore with the segment's parameter in [0, 1]). A segment parallel to
    the triangle's plane never counts."""
    direction = a1 - a0
    e1, e2 = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    p = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, p)
    ok = np.abs(det) > eps
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    s = a0 - triangles[:, 0]
    u = np.einsum("ij,ij->i", s, p) * inv
    q = np.cross(s, e1)
    v = np.einsum("ij,ij->i", direction, q) * inv
    t = np.einsum("ij,ij->i", e2, q) * inv
    tol = 1e-9
    hit: np.ndarray = (
        ok & (u >= -tol) & (v >= -tol) & (u + v <= 1 + tol) & (t >= -tol) & (t <= 1 + tol)
    )
    return hit


def _crossing_pairs(model: _Model, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Of the candidate pairs (a, b), those whose triangles cross; neighbours sharing a
    welded vertex are left out (they touch by construction)."""
    a, b = np.minimum(a, b), np.maximum(a, b)
    fa, fb = model.welded_faces[a], model.welded_faces[b]
    shared = (fa[:, :, None] == fb[:, None, :]).any(axis=(1, 2)) | (a == b)
    a, b = a[~shared], b[~shared]
    if not len(a):
        return np.zeros((0, 2), np.int64)
    va, vb = model.vertices[model.faces[a]], model.vertices[model.faces[b]]
    overlap = np.all(
        (va.min(axis=1) <= vb.max(axis=1)) & (vb.min(axis=1) <= va.max(axis=1)), axis=1
    )
    a, b, va, vb = a[overlap], b[overlap], va[overlap], vb[overlap]
    eps = (model.diagonal * 1e-12) ** 2
    hit = np.zeros(len(a), dtype=bool)
    for i in range(3):
        j = (i + 1) % 3
        hit |= _segment_hits(va[:, i], va[:, j], vb, eps)
        hit |= _segment_hits(vb[:, i], vb[:, j], va, eps)
    return np.stack([a[hit], b[hit]], axis=1)


def _ray_cells(
    grid: _Grid, origin: np.ndarray, direction: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """The grid cells along a ray in order (Amanatides and Woo, all steps at once) and
    the distance, in cells, at which the ray leaves each; stops at the grid's edge."""
    position = (origin - grid.origin) / grid.cell
    cell = np.floor(position).astype(np.int64)
    if np.any(cell < 0) or np.any(cell >= grid.dims):
        return np.zeros((0, 3), np.int64), np.zeros(0)
    step = np.where(direction >= 0, 1, -1)
    with np.errstate(divide="ignore", invalid="ignore"):
        delta = np.where(direction != 0, np.abs(1.0 / direction), np.inf)
        boundary = np.where(step > 0, cell + 1, cell) - position
        t_max = np.where(direction != 0, boundary / direction, np.inf)
    counts = np.where(direction > 0, grid.dims - cell, np.where(direction < 0, cell + 1, 0))
    times = np.concatenate([t_max[a] + delta[a] * np.arange(counts[a]) for a in range(3)])
    axes = np.repeat(np.arange(3), counts)
    order = np.argsort(times, kind="stable")  # ties: the lower axis first, as stepping does
    times, axes = times[order], axes[order]
    moves = np.zeros((len(axes) + 1, 3), np.int64)
    moves[np.arange(1, len(axes) + 1), axes] = step[axes]
    cells = cell + np.cumsum(moves, axis=0)
    outside = np.flatnonzero(np.any((cells < 0) | (cells >= grid.dims), axis=1))
    kept = int(outside[0]) if len(outside) else len(cells)
    exits = np.append(times, np.inf)
    return cells[:kept], exits[:kept]


def _ray_hits(
    grid: _Grid, origin: np.ndarray, direction: np.ndarray, *, nearest: bool, skip: int = -1
) -> tuple[float | None, np.ndarray]:
    """Walk the grid along a ray: the nearest hit's distance, or (``nearest=False``)
    the faces of every hit. ``skip``: a face to ignore."""
    model = grid.model
    cells, exits = _ray_cells(grid, origin, direction)
    keys = grid._key(cells)
    starts = np.searchsorted(grid.keys, keys)
    ends = np.searchsorted(grid.keys, keys + 1)
    seen: set[int] = {skip}
    hits: list[int] = []
    best: float | None = None
    far = origin + direction * model.diagonal * 4
    for j in np.flatnonzero(ends > starts).tolist():
        # Nothing in this cell or later can be nearer than a hit before the last exit.
        if nearest and best is not None and j > 0 and best <= float(exits[j - 1]) * grid.cell:
            break
        found = grid.owners[starts[j] : ends[j]].tolist()
        ids = np.asarray([f for f in found if f not in seen], np.int64)
        if not len(ids):
            continue
        seen.update(ids.tolist())
        tri = model.vertices[model.faces[ids]]
        ends_ = (np.broadcast_to(origin, (len(ids), 3)), np.broadcast_to(far, (len(ids), 3)))
        hit = _segment_hits(ends_[0], ends_[1], tri, 1e-300)
        if hit.any() and not nearest:
            hits.extend(ids[hit].tolist())
        elif hit.any():
            best = _nearest_plane(model, tri[hit], (origin, direction), best)
    return best, np.asarray(hits, dtype=np.int64)


def _nearest_plane(
    model: _Model, tri: np.ndarray, ray: tuple[np.ndarray, np.ndarray], best: float | None
) -> float | None:
    """The distance along the ray to the nearest hit triangle that is the far wall: one
    facing along the ray (within 60°). A side met at a glancing angle, as next to a
    sharp edge, is passed over: it is not the wall's other face."""
    origin, direction = ray
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1), 1e-300)[:, None]
    facing = normals @ direction
    far_wall = facing > 0.5
    if not far_wall.any():
        return best
    t = np.einsum("ij,ij->i", tri[far_wall, 0] - origin, normals[far_wall]) / facing[far_wall]
    t = t[t > model.diagonal * 1e-6]
    if not len(t):
        return best
    return float(t.min()) if best is None else min(best, float(t.min()))


# -- the checks -------------------------------------------------------------------------


@dataclass(slots=True)
class _Context:
    """What the checks share."""

    model: _Model
    options: Options
    findings: list[Finding] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    measurements: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {key: {} for key in CATEGORIES}
    )
    areas: np.ndarray = field(default_factory=lambda: np.zeros(0))
    normals: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    labels: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    closed: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    watertight: bool = False
    grid: _Grid | None = None

    def add(self, **kw: Any) -> None:
        self.findings.append(Finding(**kw))

    def get_grid(self) -> _Grid:
        if self.grid is None:
            self.grid = _Grid(self.model)
        return self.grid


def _attributes(ctx: _Context) -> None:
    model = ctx.model
    bad = ~np.isfinite(model.vertices).all(axis=1)
    if bad.any():
        faces = np.flatnonzero(bad[model.faces].any(axis=1))
        ctx.add(
            code="invalid_positions",
            category="integrity",
            severity="error",
            title="Invalid vertex positions",
            count=int(bad.sum()),
            unit="vertices",
            explanation="Some positions are not numbers (NaN or infinite). Viewers and "
            "exporters cannot place these triangles.",
            method="exact",
            repair="remove_degenerate_faces",
            repair_risk="Removes the triangles that use them.",
            meshes=_meshes_of(model, faces),
        )
    invalid = 0
    missing: list[str] = []
    for name, mesh in zip(model.names, model.meshes, strict=True):
        stored = _stored_normals(mesh)
        if stored is None:
            missing.append(name)
            continue
        length = np.linalg.norm(stored, axis=1)
        invalid += int((~np.isfinite(length) | (np.abs(length - 1) > 0.01)).sum())
    if invalid:
        ctx.add(
            code="invalid_normals",
            category="surface",
            severity="warning",
            title="Invalid stored normals",
            count=invalid,
            unit="vertices",
            explanation="Stored normals that are not unit length or not numbers shade wrongly.",
            method="exact",
            repair="recalculate_normals",
            repair_risk="Replaces stored normals with ones computed from the triangles; hard "
            "edges set by hand become smooth where vertices are shared.",
        )
    if missing:
        ctx.add(
            code="missing_normals",
            category="surface",
            severity="info",
            title="No stored normals",
            count=len(missing),
            unit="meshes",
            explanation="These meshes store no normals; viewers compute them from the "
            "triangles, which is usually fine.",
            method="exact",
            meshes=missing[:20],
        )


def _integrity(ctx: _Context) -> None:
    model = ctx.model
    w = model.welded_faces
    repeated = (w[:, 0] == w[:, 1]) | (w[:, 1] == w[:, 2]) | (w[:, 0] == w[:, 2])
    degenerate = np.flatnonzero(repeated | (ctx.areas <= (model.diagonal * 1e-8) ** 2))
    if len(degenerate):
        ctx.add(
            code="degenerate_faces",
            category="integrity",
            severity="warning",
            title="Zero-area triangles",
            count=len(degenerate),
            unit="triangles",
            explanation="Triangles with no area (a repeated corner, or corners in a line). "
            "They draw nothing, but break normals, smoothing and printing software.",
            method="exact",
            repair="remove_degenerate_faces",
            repair_risk="Removes them; the surface does not move. A hole can open where a "
            "zero-area triangle was the only link.",
            meshes=_meshes_of(model, degenerate),
            locations=_triangle_locations(model, degenerate),
        )
    from ai3d.processing.mesh import duplicate_face_count  # noqa: PLC0415 - avoids a cycle

    duplicates, first = duplicate_face_count(w, max(model.welded_count, 1))
    if duplicates:
        repeat = np.ones(len(model.faces), dtype=bool)
        repeat[first] = False
        faces = np.flatnonzero(repeat)
        ctx.add(
            code="duplicate_faces",
            category="integrity",
            severity="warning",
            title="Duplicate triangles",
            count=duplicates,
            unit="triangles",
            explanation="Triangles that repeat another's three corners. They flicker "
            "(z-fighting) and make edges non-manifold.",
            method="exact",
            repair="remove_duplicate_faces",
            repair_risk="Removes the repeats; the surface does not move.",
            meshes=_meshes_of(model, faces),
            locations=_triangle_locations(model, faces),
        )
    ctx.measurements["integrity"].update(
        triangles=len(model.faces),
        zero_area_triangles=len(degenerate),
        duplicate_triangles=duplicates,
    )


def _duplicate_vertices(ctx: _Context) -> None:
    """Vertices identical in position and every attribute to another in the same mesh
    (redundant), and ones sharing only the position (seams, normal splits)."""
    model = ctx.model
    redundant = 0
    for mesh in model.meshes:
        columns = [np.asarray(mesh.vertices, dtype=np.float64)]
        uv = getattr(mesh.visual, "uv", None)
        if uv is not None and len(uv) == len(mesh.vertices):
            columns.append(np.asarray(uv, dtype=np.float64))
        stored = _stored_normals(mesh)
        if stored is not None:
            columns.append(stored)
        colours = getattr(mesh.visual, "vertex_colors", None)
        if getattr(mesh.visual, "kind", None) == "vertex" and colours is not None:
            columns.append(np.asarray(colours, dtype=np.float64))
        rows = np.concatenate(columns, axis=1)
        unique, _ = trimesh.grouping.unique_rows(rows)  # type: ignore[no-untyped-call]
        redundant += len(rows) - len(unique)
    ctx.measurements["topology"]["vertices_split_at_seams"] = max(
        len(model.vertices) - model.welded_count - redundant, 0
    )
    if redundant:
        ctx.add(
            code="duplicate_vertices",
            category="topology",
            severity="info",
            title="Duplicate vertices",
            count=redundant,
            unit="vertices",
            explanation="Vertices identical in position, texture coordinates, normal and "
            "colour to another: they only add size.",
            method="exact",
            repair="merge_duplicate_vertices",
            repair_risk="Merges exact duplicates only; nothing moves and seams stay.",
        )


def _topology(ctx: _Context) -> None:
    model = ctx.model
    edges = _edge_analysis(model)
    ctx.labels = _connected(edges.unique_pairs, model.welded_count)[model.welded_faces[:, 0]]
    count = int(ctx.labels.max()) + 1
    open_piece = np.zeros(count, dtype=bool)
    open_piece[ctx.labels[edges.boundary // 3]] = True
    open_piece[ctx.labels[edges.non_manifold // 3]] = True
    ctx.closed = ~open_piece
    ctx.watertight = not len(edges.boundary) and not len(edges.non_manifold)
    ctx.measurements["topology"].update(
        watertight=ctx.watertight,
        open_edges=len(edges.boundary),
        non_manifold_edges=len(edges.non_manifold),
        pieces=count,
        closed_pieces=int(ctx.closed.sum()),
    )
    if len(edges.non_manifold):
        ctx.add(
            code="non_manifold_edges",
            category="topology",
            severity="error",
            title="Non-manifold edges",
            count=len(edges.non_manifold),
            unit="edges",
            explanation="Edges shared by three or more triangles. Most tools (printing, "
            "booleans, remeshing) need every edge between exactly two.",
            method="exact",
            repair="repair_non_manifold",
            repair_risk="Only fixed where duplicate or zero-area triangles cause it; other "
            "cases need manual editing and are reported, not changed.",
            meshes=_meshes_of(model, edges.non_manifold // 3),
            locations=_segment_locations(model, _edge_vertices(model, edges.non_manifold)),
        )
    if len(edges.boundary):
        _holes(ctx, edges.boundary)
    if len(edges.inconsistent):
        ctx.add(
            code="inconsistent_winding",
            category="topology",
            severity="warning",
            title="Inconsistent winding",
            count=len(edges.inconsistent),
            unit="edges",
            explanation="Neighbouring triangles face opposite ways across these edges, so "
            "some triangles' normals are flipped relative to their neighbours.",
            method="exact",
            repair="fix_winding",
            repair_risk="Turns triangles round to agree with their neighbours (and outward "
            "on closed pieces); nothing moves.",
            meshes=_meshes_of(model, edges.inconsistent // 3),
            locations=_segment_locations(model, _edge_vertices(model, edges.inconsistent)),
        )
    if count > 1:
        _pieces(ctx, count)


def _holes(ctx: _Context, boundary: np.ndarray) -> None:
    model = ctx.model
    loops = _hole_loops(model, boundary)
    small = sum(1 for loop in loops if loop["edges"] <= 4)
    ctx.add(
        code="open_boundaries",
        category="topology",
        severity="warning",
        title="Holes and open borders",
        count=len(loops),
        unit="openings",
        explanation=f"{len(boundary):,} edges belong to one triangle only, in {len(loops):,} "
        f"separate openings ({small:,} of three or four edges). The model is not watertight.",
        method="exact",
        repair="fill_small_holes" if small else None,
        repair_risk="Fills openings of three or four edges with flat triangles; not on "
        "textured or coloured meshes (new triangles would have no texture coordinates or "
        "colours). Larger openings are left."
        if small
        else None,
        meshes=_meshes_of(model, boundary // 3),
        locations=_segment_locations(model, _edge_vertices(model, boundary)),
        details={"open_edges": len(boundary), "largest": loops[-1], "openings": loops[:50]},
    )


def _pieces(ctx: _Context, count: int) -> None:
    model, labels = ctx.model, ctx.labels
    sizes = np.bincount(labels, minlength=count)
    sums = np.zeros((count, 3))
    for start in range(0, len(model.faces), _CHUNK):
        chunk = np.arange(start, min(start + _CHUNK, len(model.faces)))
        centres = _centres(model, chunk)
        for axis in range(3):
            sums[:, axis] += np.bincount(labels[chunk], weights=centres[:, axis], minlength=count)
    piece_centres = sums / np.maximum(sizes, 1)[:, None]
    pieces = [
        {
            "id": int(p),
            "triangles": int(sizes[p]),
            "closed": bool(ctx.closed[p]),
            "centre": _round(piece_centres[p][None])[0],
        }
        for p in np.argsort(-sizes)[:200]
    ]
    limit = max(len(model.faces) * 0.001, 1)
    tiny_faces = np.flatnonzero(sizes[labels] < limit)
    tiny = int((sizes < limit).sum())
    ctx.add(
        code="components",
        category="topology",
        severity="warning" if tiny else "info",
        title="Separate pieces",
        count=count,
        unit="pieces",
        explanation=f"The model is in {count:,} separate pieces (not connected by shared "
        f"vertices); {tiny:,} are under 0.1% of its triangles (loose fragments). Pieces can be "
        "split into separate parts, or removed one by one.",
        method="exact",
        repair="separate_components",
        repair_risk="Splitting changes no geometry. Removing pieces deletes geometry and is "
        "only done for pieces you choose.",
        locations=_triangle_locations(model, tiny_faces) if tiny else Locations(),
        details={"pieces": pieces},
    )


def _shading(ctx: _Context) -> None:
    """Inside-out closed pieces (negative volume), and stored normals that point
    against their triangles."""
    model = ctx.model
    volume = np.zeros(len(ctx.closed))
    for start in range(0, len(model.faces), _CHUNK):
        corners = model.vertices[model.faces[start : start + _CHUNK]]
        tetra = np.einsum("ij,ij->i", corners[:, 0], np.cross(corners[:, 1], corners[:, 2])) / 6
        labels = ctx.labels[start : start + _CHUNK]
        volume += np.bincount(labels, weights=tetra, minlength=len(volume))
    inside_out = np.flatnonzero(ctx.closed & (volume < 0))
    if len(inside_out):
        faces = np.flatnonzero(np.isin(ctx.labels, inside_out))
        ctx.add(
            code="inside_out",
            category="surface",
            severity="error",
            title="Inside-out pieces",
            count=len(inside_out),
            unit="pieces",
            explanation="These closed pieces have their triangles facing inwards (negative "
            "volume): they shade dark and print as holes.",
            method="exact",
            repair="fix_winding",
            repair_risk="Turns triangles round; nothing moves.",
            meshes=_meshes_of(model, faces),
            locations=_triangle_locations(model, faces),
        )
    against: list[np.ndarray] = []
    offset = 0
    for mesh in model.meshes:
        stored = _stored_normals(mesh)
        if stored is not None:
            mean = stored[np.asarray(mesh.faces)].sum(axis=1)
            facing = np.einsum("ij,ij->i", mean, ctx.normals[offset : offset + len(mesh.faces)])
            against.append(np.flatnonzero(facing < 0) + offset)
        offset += len(mesh.faces)
    faces = np.concatenate(against) if against else np.zeros(0, np.int64)
    if len(faces):
        ctx.add(
            code="normals_against_faces",
            category="surface",
            severity="warning",
            title="Stored normals point against their triangles",
            count=len(faces),
            unit="triangles",
            explanation="The normals saved with these triangles point the other way from the "
            "triangles' winding, so lighting is inverted on them.",
            method="exact",
            repair="recalculate_normals",
            repair_risk="Replaces stored normals with computed ones.",
            meshes=_meshes_of(model, faces),
            locations=_triangle_locations(model, faces),
        )


def _density(ctx: _Context) -> None:
    model, areas = ctx.model, ctx.areas
    if len(model.faces) > HEAVY_TRIANGLES:
        ctx.add(
            code="heavy",
            category="surface",
            severity="info",
            title="Very dense model",
            count=len(model.faces),
            unit="triangles",
            explanation=f"Over {HEAVY_TRIANGLES:,} triangles: slow to draw and edit on ordinary "
            "computers (the viewer uses simplified levels when the detail is not visible). "
            "Reduce it if the detail is not needed.",
            method="exact",
        )
    positive = areas[areas > 0]
    if not len(positive):
        return
    ctx.measurements["surface"].update(
        surface_area=float(areas.sum()), median_triangle_area=float(np.median(positive))
    )
    tiny = np.flatnonzero((areas > 0) & (areas < np.median(positive) * 1e-4))
    if len(tiny) > max(10, len(model.faces) * 0.001):
        ctx.add(
            code="micro_triangles",
            category="surface",
            severity="info",
            title="Very small triangles",
            count=len(tiny),
            unit="triangles",
            explanation="Triangles under 1/10,000 of the median triangle's area: they add no "
            "visible detail and can upset smoothing and printing software.",
            method="exact",
            meshes=_meshes_of(model, tiny),
            locations=_triangle_locations(model, tiny),
        )


def _self_intersections(ctx: _Context) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Pairs of triangles that cross each other: the crossing triangles, a point per
    crossing pair, and facts about the search."""
    model, grid = ctx.model, ctx.get_grid()
    found: list[np.ndarray] = []
    candidates = 0
    k = 1
    while k < 256:  # every pair of entries k apart within the same cell
        index = np.flatnonzero(grid.keys[k:] == grid.keys[:-k])
        if not len(index):
            break
        candidates += len(index)
        for start in range(0, len(index), 2_000_000):
            chunk = index[start : start + 2_000_000]
            pairs = _crossing_pairs(model, grid.owners[chunk], grid.owners[chunk + k])
            if len(pairs):
                found.append(pairs)
        k += 1
    pairs = np.unique(np.concatenate(found), axis=0) if found else np.zeros((0, 2), np.int64)
    facts = {
        "candidate_pairs": candidates,
        "crossing_pairs": len(pairs),
        "cell_size": grid.cell,
        "crowded_cells_cut_off": k >= 256,
        "long_triangles_registered_by_sampling": grid.sampled,
    }
    if not len(pairs):
        return np.zeros(0, np.int64), np.zeros((0, 3)), facts
    points = (_centres(model, pairs[:, 0]) + _centres(model, pairs[:, 1])) / 2
    return np.unique(pairs), points, facts


def _intersections(ctx: _Context) -> None:
    policy = ctx.options.self_intersections
    too_big = len(ctx.model.faces) > SELF_INTERSECTION_MAX_FACES
    if policy == "never" or (policy == "auto" and too_big):
        reason = (
            f"Not checked: over {SELF_INTERSECTION_MAX_FACES:,} triangles the search takes "
            "minutes (about 5 on 10 million). The API runs it when asked "
            '(self_intersections: "always").'
            if policy == "auto"
            else "Not checked (turned off)."
        )
        ctx.skipped.append(Skipped("self_intersections", "integrity", "Self-intersections", reason))
        return
    crossing, points, facts = _self_intersections(ctx)
    ctx.measurements["integrity"]["self_intersection_search"] = facts
    gaps = []
    if facts["crowded_cells_cut_off"]:
        gaps.append(
            "in grid cells holding over 256 triangles, only pairs near each other "
            "in the cell's list were tested"
        )
    if facts["long_triangles_registered_by_sampling"]:
        gaps.append(
            f"{facts['long_triangles_registered_by_sampling']:,} very long triangles "
            "were placed in the grid by sampling and may miss cells they only graze"
        )
    if gaps:
        ctx.skipped.append(
            Skipped(
                "self_intersections_partial",
                "integrity",
                "Self-intersections: search not complete",
                "Crossings may be missed: " + "; ".join(gaps) + ".",
            )
        )
    if not len(crossing):
        return
    ctx.add(
        code="self_intersections",
        category="integrity",
        severity="warning",
        title="Self-intersections",
        count=facts["crossing_pairs"],
        unit="crossing pairs",
        explanation=f"{len(crossing):,} triangles pass through other triangles of the model "
        "(neighbours sharing a corner are not counted). Printing and boolean tools may fail or "
        "fill the wrong side.",
        method="approximate",
        meshes=_meshes_of(ctx.model, crossing),
        locations=_point_locations(points, facts["crossing_pairs"]),
        details={
            "limits": "Exact segment-triangle tests between triangles that share a grid cell. "
            "Not detected: triangles lying in the same plane and overlapping, and crossings "
            "between neighbours that share a corner."
        },
    )


def inspect_scene(scene: trimesh.Scene, options: Options | None = None) -> Report:
    """Inspect a loaded model (see the module notes)."""
    started = time.perf_counter()
    ctx = _Context(model=_gather(scene), options=options or Options())
    if not len(ctx.model.faces):
        ctx.add(
            code="empty",
            category="integrity",
            severity="error",
            title="No triangles",
            count=0,
            unit="triangles",
            explanation="The model holds no triangle meshes.",
            method="exact",
        )
        return _report(ctx, started)
    _attributes(ctx)
    ctx.areas, ctx.normals = _areas_normals(ctx.model)
    _integrity(ctx)
    _duplicate_vertices(ctx)
    _topology(ctx)
    _shading(ctx)
    _density(ctx)
    _intersections(ctx)
    if ctx.options.printability:
        _fabrication(ctx)
    return _report(ctx, started)


def _report(ctx: _Context, started: float) -> Report:
    _sort(ctx.findings)
    return Report(
        report_version=REPORT_VERSION,
        triangles=len(ctx.model.faces),
        vertices=len(ctx.model.vertices),
        meshes=len(ctx.model.meshes),
        findings=ctx.findings,
        skipped=ctx.skipped,
        measurements=ctx.measurements,
        options=asdict(ctx.options),
        seconds=round(time.perf_counter() - started, 2),
    )


# -- fabrication -------------------------------------------------------------------------


def _fabrication(ctx: _Context) -> None:
    """3D-printing checks. Watertight is necessary, never sufficient."""
    fab = ctx.measurements["fabrication"]
    fab["units"] = "mm" if ctx.options.units_per_mm else "model units"
    fab["watertight"] = ctx.watertight
    if not ctx.watertight:
        ctx.add(
            code="not_watertight",
            category="fabrication",
            severity="error",
            title="Not watertight",
            count=1,
            unit="model",
            explanation="A printer needs a closed surface to know what is inside; this one has "
            "open or non-manifold edges (see Topology). Wall thickness was not measured.",
            method="exact",
        )
    shells = int(ctx.closed.sum())
    fab["closed_shells"] = shells
    if shells > 1:
        ctx.add(
            code="multiple_shells",
            category="fabrication",
            severity="info",
            title="Several shells",
            count=shells,
            unit="shells",
            explanation="Several closed pieces: each prints as its own object unless they touch "
            "or are joined.",
            method="exact",
        )
    _overhangs(ctx)
    if ctx.watertight:
        _wall_thickness(ctx)
        _internal_shells(ctx)


def _overhangs(ctx: _Context) -> None:
    """Triangles facing down more steeply than the limit (glTF: +Y is up), except those
    on the build plate."""
    model, areas, normals = ctx.model, ctx.areas, ctx.normals
    degrees = ctx.options.overhang_degrees
    limit = math.cos(math.radians(90 - degrees))
    y = model.vertices[:, 1]
    centre_y = (y[model.faces[:, 0]] + y[model.faces[:, 1]] + y[model.faces[:, 2]]) / 3
    resting = centre_y <= y.min() + model.diagonal * 0.002
    overhang = np.flatnonzero((normals[:, 1] < -limit) & ~resting)
    share = float(areas[overhang].sum() / max(areas.sum(), 1e-300))
    ctx.measurements["fabrication"]["overhang_area_share"] = round(share, 4)
    if not len(overhang):
        return
    ctx.add(
        code="overhangs",
        category="fabrication",
        severity="warning" if share > 0.05 else "info",
        title=f"Overhangs steeper than {degrees:g}°",
        count=len(overhang),
        unit="triangles",
        explanation=f"{share:.1%} of the surface faces down more steeply than {degrees:g}° from "
        "the build direction (+Y, as stored), which usually needs supports on filament and "
        "resin printers.",
        method="exact",
        meshes=_meshes_of(model, overhang),
        locations=_triangle_locations(model, overhang),
        details={
            "measured": "the angle is exact; whether supports are needed depends on the "
            "printer, the material and the orientation"
        },
    )


def _wall_thickness(ctx: _Context) -> None:
    model, options = ctx.model, ctx.options
    fab = ctx.measurements["fabrication"]
    unit = fab["units"]
    to_unit = (1 / options.units_per_mm) if options.units_per_mm else 1.0
    grid = ctx.get_grid()
    rng = np.random.default_rng(0)
    count = THICKNESS_SAMPLES  # with replacement: as many on a small model as a large one
    chosen = rng.choice(len(model.faces), size=count, p=ctx.areas / ctx.areas.sum())
    weights = rng.dirichlet(np.ones(3), size=count)
    points = np.einsum("ij,ijk->ik", weights, model.vertices[model.faces[chosen]])
    # Two directions per point, keeping the longer reading: the triangle's own normal
    # (right on crisp shapes; at a box's edge a smoothed normal points diagonally) and
    # the smoothed surface normal, area-weighted over the welded corners and
    # interpolated (right on noisy scans, where one triangle can tilt by tens of
    # degrees). A thin wall reads thin either way.
    smooth = np.zeros((model.welded_count, 3))
    weighted = ctx.normals * ctx.areas[:, None]
    for k in range(3):
        for axis in range(3):
            smooth[:, axis] += np.bincount(
                model.welded_faces[:, k], weights=weighted[:, axis], minlength=model.welded_count
            )
    smoothed = np.einsum("ij,ijk->ik", weights, smooth[model.welded_faces[chosen]])
    smoothed /= np.maximum(np.linalg.norm(smoothed, axis=1), 1e-300)[:, None]
    thickness = np.full(count, np.nan)
    for i in range(count):
        own = ctx.normals[chosen[i]]
        readings = []
        for n in (own, smoothed[i]) if float(own @ smoothed[i]) < 0.999 else (own,):
            start = points[i] - n * model.diagonal * 1e-7
            distance, _ = _ray_hits(grid, start, -n, nearest=True, skip=int(chosen[i]))
            if distance is not None:
                readings.append(distance)
        if readings:
            thickness[i] = max(readings) * to_unit
    measured = thickness[np.isfinite(thickness)]
    stats = {
        "samples": count,
        "measured": len(measured),
        "unit": unit,
        "method": "sampled: from each sample point, rays straight into the model along the "
        "triangle's normal and the smoothed surface normal, to the first surface facing "
        "the other way; the longer reading is kept",
    }
    for name, value in (("min", 0.0), ("p05", 5.0), ("median", 50.0)):
        stats[name] = round(float(np.percentile(measured, value)), 6) if len(measured) else None
    fab["wall_thickness"] = stats
    fab["minimum_feature_size"] = stats["min"]
    threshold = options.min_wall_mm
    thin = np.flatnonzero(np.isfinite(thickness) & (thickness < threshold))
    if not len(thin):
        return
    note = (
        ""
        if options.units_per_mm
        else "; the model has no real-world scale, so this compares model units with the "
        "number you gave"
    )
    ctx.add(
        code="thin_walls",
        category="fabrication",
        severity="warning",
        title="Thin walls or features",
        count=len(thin),
        unit="sample points",
        explanation=f"{len(thin) / max(len(measured), 1):.1%} of {len(measured):,} measured "
        f"points are under {threshold:g} {unit} thick (to the next surface, straight inwards)"
        f"{note}. Thin walls and small features may not print or may break.",
        method="sampled",
        locations=_point_locations(points[thin], len(thin)),
        details={
            "min_wall": threshold,
            "unit": unit,
            "limits": "Rays along the triangle's normal and the smoothed normal, to the first "
            "surface facing the other way (within 60°), keeping the longer: this can "
            "overestimate by up to about 40% where the two directions differ, and at sharp "
            "tips; between sample points nothing is measured. Where the surface folds "
            "through itself (self-intersections), readings there are not meaningful.",
        },
    )


def _internal_shells(ctx: _Context) -> None:
    """Closed pieces whose first vertex is inside another closed piece (an odd number
    of crossings of a ray through that piece)."""
    closed_ids = np.flatnonzero(ctx.closed)
    if len(closed_ids) > 200:
        ctx.measurements["fabrication"]["internal_shells"] = "not checked: over 200 pieces"
        return
    if len(closed_ids) < 2:
        return
    model, labels, grid = ctx.model, ctx.labels, ctx.get_grid()
    inside: list[int] = []
    for piece in closed_ids:
        face = int(np.flatnonzero(labels == piece)[0])
        origin = model.vertices[model.faces[face, 0]] + model.diagonal * 1e-6
        _, hits = _ray_hits(grid, origin, _OBLIQUE, nearest=False)
        others = labels[hits]
        odd = [o for o in np.unique(others[others != piece]) if int((others == o).sum()) % 2]
        if odd:
            inside.append(int(piece))
    if not inside:
        return
    faces = np.flatnonzero(np.isin(labels, inside))
    ctx.add(
        code="internal_shells",
        category="fabrication",
        severity="warning",
        title="Shells inside other shells",
        count=len(inside),
        unit="shells",
        explanation="Closed pieces lying inside another piece: hidden geometry that slicers "
        "may treat as a void or as solid.",
        method="approximate",
        meshes=_meshes_of(model, faces),
        locations=_triangle_locations(model, faces),
        details={
            "limits": "One ray per piece, counting crossings; a ray grazing an edge can miscount."
        },
    )


# -- UVs, materials and textures -------------------------------------------------------


def _glb_document(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
            if len(header) < 20 or header[:4] != b"glTF":
                return None
            length, kind = struct.unpack("<II", header[12:20])
            if kind != 0x4E4F534A:
                return None
            document: dict[str, Any] = json.loads(handle.read(length))
            return document
    except (OSError, ValueError, struct.error):
        return None


def _texture_slots(material: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    pbr = material.get("pbrMetallicRoughness", {})
    slots = [(n, pbr[n]) for n in ("baseColorTexture", "metallicRoughnessTexture") if n in pbr]
    slots += [
        (n, material[n])
        for n in ("normalTexture", "occlusionTexture", "emissiveTexture")
        if n in material
    ]
    return slots


def _references(document: dict[str, Any]) -> tuple[list[str], set[int]]:
    """References to missing materials, textures or images, and the materials used."""
    materials = document.get("materials", [])
    textures = document.get("textures", [])
    images = document.get("images", [])
    used: set[int] = set()
    bad: list[str] = []
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            index = primitive.get("material")
            if index is None:
                continue
            if isinstance(index, int) and 0 <= index < len(materials):
                used.add(index)
            else:
                bad.append(f"mesh “{mesh.get('name', '?')}” → material {index}")
    for i, material in enumerate(materials):
        for slot, info in _texture_slots(material):
            ref = info.get("index")
            if not isinstance(ref, int) or not 0 <= ref < len(textures):
                bad.append(f"material “{material.get('name', i)}” {slot} → texture {ref}")
                continue
            source = textures[ref].get("source")
            if not isinstance(source, int) or not 0 <= source < len(images):
                bad.append(f"texture {ref} → image {source}")
    return bad, used


def _material_findings(add: Add, document: dict[str, Any]) -> None:
    materials = document.get("materials", [])
    bad, used = _references(document)
    if bad:
        add(
            code="invalid_references",
            category="uv_texture",
            severity="error",
            title="Invalid material references",
            count=len(bad),
            unit="references",
            explanation="References to materials, textures or images the file does not "
            "contain; those surfaces fall back to defaults.",
            method="exact",
            details={"references": bad[:50]},
        )
    unused = [m.get("name", f"#{i}") for i, m in enumerate(materials) if i not in used]
    if unused:
        add(
            code="unused_materials",
            category="uv_texture",
            severity="info",
            title="Unused materials",
            count=len(unused),
            unit="materials",
            explanation="Materials no mesh uses; they only add size.",
            method="exact",
            details={"materials": unused[:50]},
        )
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for i, material in enumerate(materials):
        key = json.dumps({k: v for k, v in material.items() if k != "name"}, sort_keys=True)
        if key in seen:
            duplicates.append(f"“{material.get('name', i)}” = “{seen[key]}”")
        else:
            seen[key] = str(material.get("name", i))
    if duplicates:
        add(
            code="duplicate_materials",
            category="uv_texture",
            severity="info",
            title="Duplicate materials",
            count=len(duplicates),
            unit="materials",
            explanation="Materials identical to another (apart from the name); they could be one.",
            method="exact",
            details={"materials": duplicates[:50]},
        )


def _image_size(path: Path, document: dict[str, Any], image: dict[str, Any]) -> dict[str, Any]:
    from PIL import Image  # noqa: PLC0415

    view_index = image.get("bufferView")
    if not isinstance(view_index, int):
        return {"width": None, "height": None, "reason": "external or missing data"}
    try:
        view = document["bufferViews"][view_index]
        with path.open("rb") as handle:
            length = struct.unpack("<I", handle.read(16)[12:16])[0]
            handle.seek(20 + length + 8 + int(view.get("byteOffset", 0)))
            head = handle.read(min(int(view["byteLength"]), 512 * 1024))
        with Image.open(io.BytesIO(head)) as opened:
            width, height = opened.size
            return {"width": width, "height": height, "format": opened.format}
    except Exception as exc:  # any unreadable image is reported, not raised
        return {"width": None, "height": None, "reason": type(exc).__name__}


def _image_findings(add: Add, path: Path, document: dict[str, Any]) -> list[dict[str, Any]]:
    facts = []
    for i, image in enumerate(document.get("images", [])):
        fact = _image_size(path, document, image)
        fact["index"] = i
        fact["name"] = image.get("name") or image.get("uri") or f"image {i}"
        facts.append(fact)
    missing = [f["name"] for f in facts if f.get("width") is None]
    if missing:
        add(
            code="missing_textures",
            category="uv_texture",
            severity="error",
            title="Missing or unreadable textures",
            count=len(missing),
            unit="images",
            explanation="Images the file refers to but that cannot be read (missing data, an "
            "external file, or an unsupported format).",
            method="exact",
            details={"images": missing[:50]},
        )
    large = [f["name"] for f in facts if max(f.get("width") or 0, f.get("height") or 0) > 8192]
    if large:
        add(
            code="huge_textures",
            category="uv_texture",
            severity="warning",
            title="Textures over 8192 px",
            count=len(large),
            unit="images",
            explanation="Many graphics cards cannot load textures over 8192 pixels a side.",
            method="exact",
            details={"images": large},
        )
    return facts


@dataclass(slots=True)
class _UvStats:
    missing: list[str] = field(default_factory=list)
    degenerate: int = 0
    distorted_area: float = 0.0
    total_area: float = 0.0
    used_cells: int = 0
    overlap_cells: int = 0
    overlap_skipped: list[str] = field(default_factory=list)  # meshes too large to check


def _has_image(visual: Any) -> bool:
    material = getattr(visual, "material", None)
    return any(getattr(material, a, None) is not None for a in ("baseColorTexture", "image"))


def _uv_overlap(
    faces: np.ndarray, uv: np.ndarray, vertices: np.ndarray, size: int = 512
) -> tuple[int, int]:
    """Grid cells of the texture square used by triangles of two or more UV islands
    (sampled at each triangle's centre and towards its corners), and cells used."""
    if len(faces) > UV_OVERLAP_MAX_FACES:
        return -1, 0
    # UV islands: triangles connected through vertices that share both the position and
    # the texture coordinates (copies of a part with the same UVs are separate islands).
    extent = float(np.ptp(vertices, axis=0).max()) or 1.0
    rows = np.concatenate(
        [np.round(uv * 1e6), np.round(vertices / (extent * 1e-7))], axis=1
    ).astype(np.int64)
    _, ids = trimesh.grouping.unique_rows(rows)  # type: ignore[no-untyped-call]
    tri = np.asarray(ids)[faces]
    pairs = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]]])
    island = _connected(pairs, int(tri.max()) + 1)[tri[:, 0]]
    islands = int(island.max()) + 1
    found = []
    for start in range(0, len(faces), _CHUNK):  # a block at a time: no full-size samples
        corners = uv[faces[start : start + _CHUNK]]
        centre = corners.mean(axis=1, keepdims=True)
        samples = np.concatenate([centre, (corners * 3 + centre) / 4], axis=1)
        cell = np.clip(np.floor(np.mod(samples, 1.0) * size).astype(np.int64), 0, size - 1)
        keys = cell[:, :, 0] * size + cell[:, :, 1]
        found.append(np.unique(keys * islands + island[start : start + _CHUNK, None]))
    used = np.unique(np.concatenate(found))
    _, per_cell = np.unique(used // islands, return_counts=True)
    return len(per_cell), int((per_cell > 1).sum())


def _mesh_uvs(name: str, mesh: trimesh.Trimesh, stats: _UvStats) -> None:
    visual = mesh.visual
    uv = getattr(visual, "uv", None)
    has_uv = uv is not None and len(uv) == len(mesh.vertices)
    if getattr(visual, "kind", None) == "texture" and _has_image(visual) and not has_uv:
        stats.missing.append(name)
        return
    if not has_uv:
        return
    uv = np.asarray(uv, dtype=np.float64)
    faces, positions = np.asarray(mesh.faces), np.asarray(mesh.vertices)
    uv_area, area = np.empty(len(faces)), np.empty(len(faces))
    for start in range(0, len(faces), _CHUNK):  # a block at a time: no full-size corners
        block = faces[start : start + _CHUNK]
        corners = uv[block]
        a, b = corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]
        uv_area[start : start + _CHUNK] = np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) / 2
        xyz = positions[block]
        cross = np.cross(xyz[:, 1] - xyz[:, 0], xyz[:, 2] - xyz[:, 0])
        area[start : start + _CHUNK] = np.linalg.norm(cross, axis=1) / 2
    live = area > 0
    stats.degenerate += int((live & (uv_area <= 1e-14)).sum())
    if uv_area.sum() > 0 and area.sum() > 0:
        ratio = (area / area.sum()) / np.maximum(uv_area / uv_area.sum(), 1e-300)
        stretched = live & ((ratio > 4) | (ratio < 0.25))
        stats.distorted_area += float(area[stretched].sum())
        stats.total_area += float(area.sum())
    used, overlap = _uv_overlap(faces, uv, positions)
    if used < 0:
        stats.overlap_skipped.append(name)
        return
    stats.used_cells += used
    stats.overlap_cells += overlap


def _uv_findings(add: Add, scene: trimesh.Scene, report: Report) -> None:
    measures = report.measurements["uv_texture"]
    stats = _UvStats()
    for name, mesh in scene.geometry.items():
        if isinstance(mesh, trimesh.Trimesh) and len(mesh.faces):
            _mesh_uvs(str(name), mesh, stats)
    if stats.overlap_skipped:
        report.skipped.append(
            Skipped(
                "uv_overlaps",
                "uv_texture",
                "Overlapping UV islands",
                f"Not checked on meshes over {UV_OVERLAP_MAX_FACES:,} triangles (memory): "
                + ", ".join(stats.overlap_skipped[:10]),
            )
        )
    if stats.missing:
        add(
            code="missing_uvs",
            category="uv_texture",
            severity="error",
            title="Textured meshes without UVs",
            count=len(stats.missing),
            unit="meshes",
            explanation="These meshes have a texture but no texture coordinates, so the texture "
            "cannot be placed.",
            method="exact",
            meshes=stats.missing[:20],
        )
    if stats.degenerate:
        add(
            code="degenerate_uvs",
            category="uv_texture",
            severity="warning",
            title="Collapsed UV triangles",
            count=stats.degenerate,
            unit="triangles",
            explanation="Triangles with area on the model but none in texture space: the whole "
            "triangle shows one texel (streaks).",
            method="exact",
        )
    if stats.total_area:
        share = stats.distorted_area / stats.total_area
        measures["uv_distortion_share"] = round(share, 4)
        if share > 0.02:
            add(
                code="uv_distortion",
                category="uv_texture",
                severity="warning" if share > 0.1 else "info",
                title="Stretched UVs",
                count=round(share * 100, 1),
                unit="% of the surface",
                explanation="Where a triangle's share of the texture differs from its share of "
                "the surface by more than 4×, the texture looks stretched or blurred.",
                method="exact",
                details={"rule": "area ratio over 4 or under 1/4, per triangle, against the mesh"},
            )
    if stats.used_cells:
        measures["uv_overlap_share"] = round(stats.overlap_cells / stats.used_cells, 4)
    # Cells on the border between neighbouring islands always hold both; under 1% is
    # that, not overlap.
    if stats.used_cells and stats.overlap_cells / stats.used_cells > 0.01:
        add(
            code="uv_overlaps",
            category="uv_texture",
            severity="info",
            title="Overlapping UV islands",
            count=round(100 * stats.overlap_cells / stats.used_cells, 1),
            unit="% of the used texture",
            explanation="Parts of the texture are used by more than one separate UV island. "
            "Intended for mirrored or repeated parts; a problem for baking and unique "
            "texturing.",
            method="approximate",
            details={
                "limits": "Measured on a 512 × 512 grid of the texture square. Not detected: "
                "overlaps within one island (folds) and ones smaller than a grid cell."
            },
        )


def inspect_materials(path: Path, scene: trimesh.Scene, report: Report) -> None:
    """Add UV, material and texture findings to ``report`` (from the file's own
    references and the loaded meshes)."""

    def add(**kw: Any) -> None:
        report.findings.append(Finding(**kw))

    document = _glb_document(path) or {}
    measures = report.measurements["uv_texture"]
    _material_findings(add, document)
    measures["images"] = _image_findings(add, path, document)
    _uv_findings(add, scene, report)
    _sort(report.findings)


# -- running it on a file (in a separate process; see services/inspection.py) ---------


def inspect_file(path: str, options: dict[str, Any]) -> dict[str, Any]:
    """Load a GLB, inspect it, and return the report as plain data. A file that cannot
    be opened is reported (with what its own description says is wrong), not raised."""
    from ai3d.processing.mesh import load_model_scene  # noqa: PLC0415

    started = time.perf_counter()
    try:
        scene = load_model_scene(Path(path))
    except Exception as exc:  # trimesh raises many types for broken files
        ctx = _Context(model=_empty_model(), options=Options(**options))
        ctx.add(
            code="unreadable",
            category="integrity",
            severity="error",
            title="The model could not be opened",
            count=1,
            unit="file",
            explanation=f"The geometry could not be read ({type(exc).__name__}); the checks "
            "below come from the file's own description only.",
            method="exact",
        )
        report = _report(ctx, started)
        document = _glb_document(Path(path)) or {}
        _material_findings(lambda **kw: report.findings.append(Finding(**kw)), document)
        _sort(report.findings)
        return report.to_dict()
    report = inspect_scene(scene, Options(**options))
    inspect_materials(Path(path), scene, report)
    return report.to_dict()


def markdown(report: dict[str, Any], title: str) -> str:
    """The report as a readable document."""
    lines = [
        f"# Model health report: {title}",
        "",
        f"{report['triangles']:,} triangles, {report['vertices']:,} vertices, "
        f"{report['meshes']} mesh(es). Inspected in {report['seconds']} s "
        f"(report version {report['report_version']}).",
        "",
        "Counts are exact unless marked approximate or sampled; locations are listed for at "
        f"most {MAX_LOCATIONS:,} elements per finding. There is no overall score: each "
        "section lists what was found.",
        "",
    ]
    for key, label in CATEGORIES.items():
        lines += _markdown_section(report, key, label)
    return "\n".join(lines) + "\n"


def _markdown_section(report: dict[str, Any], key: str, label: str) -> list[str]:
    found = [f for f in report["findings"] if f["category"] == key]
    skipped = [s for s in report["skipped"] if s["category"] == key]
    measures = report["measurements"].get(key) or {}
    if not found and not skipped and not measures:
        return []
    lines = [f"## {label}", ""]
    if not found:
        lines += ["No problems found by the checks that ran.", ""]
    for f in found:
        repair = f" Repair: {f['repair']} ({f['repair_risk']})" if f.get("repair") else ""
        lines.append(
            f"- **{f['severity'].upper()}: {f['title']}**: {f['count']:,} {f['unit']} "
            f"({f['method']}). {f['explanation']}{repair}"
        )
        if f.get("meshes"):
            lines.append(f"  Meshes: {', '.join(f['meshes'])}")
    lines += [f"- {s['title']}: {s['reason']}" for s in skipped]
    if measures:
        lines += ["", "Measurements:", ""]
        lines += [f"- {k}: {json.dumps(v)}" for k, v in measures.items() if k != "images"]
        lines += [
            f"- texture {image['name']}: {image.get('width')} × {image.get('height')} px"
            for image in measures.get("images", [])
        ]
    lines.append("")
    return lines
