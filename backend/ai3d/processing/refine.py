"""Refining a locally generated (TripoSR) model: our own post-processing.

TripoSR returns a dense marching-cubes surface (about 200,000 triangles at its
highest setting) with one colour per vertex, and often a few loose fragments.
This turns it into a lighter, textured model:

1. **Fragments**: loose pieces smaller than ``FRAGMENT_SHARE`` of the largest
   piece's area are removed (stray specks around the object).
2. **Smoothing**: a few Taubin passes, which do not shrink the model, take out
   the grid's stair-steps and small lumps.
3. **Simplifying** to the target triangle count (:func:`lod.simplify_mesh`), then
   mending what simplifying can break: back-to-back triangles and edges shared by
   three or more triangles (:func:`close_up`), so a closed model stays closed.
4. **Unwrapping**: triangles are grouped into charts by the direction they face,
   each chart is projected flat and the charts are packed into a square texture.
5. **Baking**: each texel takes the dense mesh's colour at that point. Where the
   input camera saw the surface, the photo itself is projected on: much sharper
   than per-vertex colours. It fades out towards grazing angles and is not used
   on hidden parts.

The input camera is TripoSR's training camera (``CAMERA_DISTANCE``,
``CAMERA_FOV_DEG``, looking at the front along -Z in glTF axes). The model's
outline never matches the cut-out exactly, so before projecting, a scale and
offset are fitted that line the outline up with the cut-out (:func:`fit_camera`);
if they cannot be lined up (``MIN_FIT_IOU``), the photo is not projected.

Everything runs on the CPU with NumPy, SciPy, OpenCV and trimesh; nothing is
downloaded and no third-party model is involved.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import cv2
import numpy as np
import trimesh
from PIL import Image
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from ai3d.processing.lod import simplify_mesh

# Loose pieces under this share of the largest piece's surface area are removed.
FRAGMENT_SHARE = 0.01
SMOOTH_ITERATIONS = 10
# TripoSR's input camera (its training renders): distance from the centre and
# vertical field of view. The front faces +Z (glTF axes, Y up).
CAMERA_DISTANCE = 1.9
CAMERA_FOV_DEG = 40.0
# The photo is projected at full strength where the surface faces the camera within
# PHOTO_FULL_DEG, fading to nothing at PHOTO_NONE_DEG.
PHOTO_FULL_DEG = 45.0
PHOTO_NONE_DEG = 75.0
# A texel counts as seen by the camera when nothing lies in front of it by more than
# this share of the camera distance.
VISIBILITY_TOLERANCE = 0.004
# The model's outline must overlap the cut-out at least this well (intersection over
# union, after fitting) for the photo to be projected.
MIN_FIT_IOU = 0.6
FIT_SIZE = 256
VISIBILITY_SIZE = 1024
# Empty texels around each chart, filled with the chart's edge colours so that
# texture filtering does not pull in the background.
GUTTER = 4
PHOTO_MAX_SIZE = 2048
ROUGHNESS = 0.9


@dataclass(frozen=True, slots=True)
class RefineSettings:
    triangles: int
    texture_size: int
    smooth_iterations: int = SMOOTH_ITERATIONS


@dataclass(slots=True)
class RefineReport:
    triangles_before: int = 0
    triangles_after: int = 0
    edges_closed: int = 0  # edges shared by 3+ triangles after simplifying, mended
    fragments_removed: int = 0
    fragment_area_share: float = 0.0
    charts: int = 0
    texture_size: int = 0
    texture_fill: float = 0.0  # share of the texture used by the charts
    photo_projected: bool = False
    photo_fit_iou: float | None = None
    photo_share: float = 0.0  # share of the surface coloured (mostly) from the photo
    note: str | None = None
    seconds: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CameraFit:
    """Image position = ``scale`` × (pinhole position - centre) + centre + offset, in
    pixels of a ``size`` × ``size`` picture."""

    scale: float
    offset: tuple[float, float]
    iou: float
    size: int

    def to_pixels(self, points: np.ndarray, size: int) -> np.ndarray:
        """Pixel positions (x right, y down) of glTF points in a ``size`` picture."""
        base = _pinhole(points, size)
        centre = size / 2.0
        factor = size / self.size
        return np.stack(
            [
                self.scale * (base[:, 0] - centre) + centre + self.offset[0] * factor,
                self.scale * (base[:, 1] - centre) + centre + self.offset[1] * factor,
            ],
            axis=1,
        )


# --- geometry ---------------------------------------------------------------------


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0.0, 1.0)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def _vertex_colours_srgb(mesh: trimesh.Trimesh) -> np.ndarray:
    """Per-vertex colours in sRGB 0..1 (glTF stores vertex colours as linear)."""
    visual = mesh.visual
    colours = None
    if isinstance(visual, trimesh.visual.ColorVisuals) and visual.kind == "vertex":
        colours = np.asarray(visual.vertex_colors)
    if colours is None:
        colours = np.full((len(mesh.vertices), 4), 200, dtype=np.uint8)
    return linear_to_srgb(colours[:, :3].astype(np.float64) / 255.0)


def remove_fragments(mesh: trimesh.Trimesh) -> tuple[trimesh.Trimesh, int, float]:
    """Drop loose pieces smaller than FRAGMENT_SHARE of the largest piece's area.

    Returns the mesh, how many pieces were removed and their share of the area.
    """
    count = len(mesh.faces)
    labels = trimesh.graph.connected_component_labels(  # type: ignore[no-untyped-call]
        mesh.face_adjacency, node_count=count
    )
    areas = np.bincount(labels, weights=mesh.area_faces)
    keep_label = areas >= FRAGMENT_SHARE * areas.max()
    removed = int((~keep_label).sum())
    if removed == 0:
        return mesh, 0, 0.0
    keep = keep_label[labels]
    share = float(areas[~keep_label].sum() / max(areas.sum(), 1e-12))
    cleaned = mesh.copy()
    cleaned.update_faces(keep)
    cleaned.remove_unreferenced_vertices()
    return cleaned, removed, share


def smooth(mesh: trimesh.Trimesh, iterations: int) -> trimesh.Trimesh:
    """Taubin smoothing (does not shrink the model); returns a new mesh without
    stored normals, so that later steps compute them from the smoothed surface."""
    visual = mesh.visual
    colours = (
        visual.vertex_colors
        if isinstance(visual, trimesh.visual.ColorVisuals) and visual.kind == "vertex"
        else None
    )
    result = trimesh.Trimesh(
        vertices=np.array(mesh.vertices, dtype=np.float64),
        faces=np.array(mesh.faces),
        vertex_colors=colours,
        process=False,
    )
    if iterations > 0:
        trimesh.smoothing.filter_taubin(  # type: ignore[no-untyped-call]
            result, lamb=0.5, nu=0.53, iterations=iterations
        )
    return result


# --- keeping the simplified surface closed ----------------------------------------
#
# Simplifying can fold thin parts flat: it leaves pairs of identical back-to-back
# triangles and edges shared by three or more triangles, so a closed model stops
# being closed (3D-printing tools reject it, and inside/outside cannot be told).
# Back-to-back pairs are removed; at other such edges the triangles around the edge
# are removed and each resulting hole is closed with a small fan around its centre.

MANIFOLD_PASSES = 6


def _edge_counts(faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per face corner edge (3F, in face order 01, 12, 20): its index among the
    unique edges; and how many triangles use each unique edge."""
    edges = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    _, inverse, counts = np.unique(edges, axis=0, return_inverse=True, return_counts=True)
    return inverse.reshape(-1), counts


def _without_back_to_back(faces: np.ndarray) -> np.ndarray:
    _, inverse, counts = np.unique(
        np.sort(faces, axis=1), axis=0, return_inverse=True, return_counts=True
    )
    keep: np.ndarray = faces[counts[inverse.reshape(-1)] == 1]
    return keep


def _simple_loops(loop: list[int]) -> list[list[int]]:
    """Split a hole's outline that passes a vertex more than once into simple loops."""
    loops: list[list[int]] = []
    path: list[int] = []
    position: dict[int, int] = {}
    for vertex in loop:
        if vertex in position:
            start = position[vertex]
            piece = path[start:]
            for other in piece[1:]:
                position.pop(other, None)
            del path[start + 1 :]
            if len(piece) >= 3:
                loops.append(piece)
        else:
            position[vertex] = len(path)
            path.append(vertex)
    if len(path) >= 3:
        loops.append(path)
    return loops


def _holes(faces: np.ndarray) -> list[list[int]]:
    """Each hole's outline as simple loops of vertices, in the direction of the
    triangles around it (edges used by one triangle, followed edge by edge)."""
    half = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    corner_edge, counts = _edge_counts(faces)
    border = half[counts[corner_edge] == 1]
    leaving: dict[int, list[int]] = {}
    for index, (start, _) in enumerate(border):
        leaving.setdefault(int(start), []).append(index)
    used = np.zeros(len(border), dtype=bool)
    loops: list[list[int]] = []
    for first in range(len(border)):
        if used[first]:
            continue
        loop: list[int] = []
        index = first
        while not used[index]:
            used[index] = True
            loop.append(int(border[index][0]))
            onward = [k for k in leaving.get(int(border[index][1]), []) if not used[k]]
            if not onward:
                break
            index = onward[0]
        if int(border[index][1]) == loop[0]:
            loops.extend(_simple_loops(loop))
    return loops


def close_up(
    vertices: np.ndarray, faces: np.ndarray, normals: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Remove back-to-back triangles and edges shared by more than two triangles,
    closing the holes this leaves. Returns vertices, faces, normals and how many such
    edges there were at first."""
    found = int((_edge_counts(faces)[1] > 2).sum())
    faces = _without_back_to_back(faces)
    for _ in range(MANIFOLD_PASSES):
        corner_edge, counts = _edge_counts(faces)
        crowded = counts[corner_edge].reshape(3, -1).T.max(axis=1) > 2
        if not crowded.any():
            break
        faces = faces[~crowded]
        new_points: list[np.ndarray] = []
        new_normals: list[np.ndarray] = []
        patches: list[np.ndarray] = [faces]
        for loop in _holes(faces):
            ring = np.array(loop)
            centre = len(vertices) + len(new_points)
            new_points.append(vertices[ring].mean(axis=0))
            normal = normals[ring].sum(axis=0)
            new_normals.append(normal / max(float(np.linalg.norm(normal)), 1e-12))
            # The border runs a → b along the existing triangles; patches run b → a.
            patches.append(np.stack([np.roll(ring, -1), ring, np.full(len(ring), centre)], axis=1))
        if new_points:
            vertices = np.vstack([vertices, np.array(new_points)])
            normals = np.vstack([normals, np.array(new_normals)])
        faces = np.concatenate(patches)
    return vertices, faces, normals, found


# --- unwrapping -------------------------------------------------------------------

_AXES = np.array(
    [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]], dtype=np.float64
)


def _basis(direction: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two axes spanning the plane facing ``direction``, so that u × v = direction
    (triangles keep their winding in the chart)."""
    helper = np.array([0.0, 1.0, 0.0]) if abs(direction[1]) < 0.9 else np.array([0.0, 0.0, 1.0])
    u = np.cross(helper, direction)
    u /= np.linalg.norm(u)
    return u, np.cross(direction, u)


@dataclass(frozen=True, slots=True)
class Unwrapped:
    vertex_of: np.ndarray  # per unwrapped vertex: its vertex in the source mesh
    faces: np.ndarray  # faces on the unwrapped vertices
    uv_pixels: np.ndarray  # per unwrapped vertex: position in the texture (x right, y down)
    size: int
    charts: int
    fill: float


def _charts(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
    """Per face: its chart, and its chart's facing axis (index into _AXES)."""
    facing = np.argmax(mesh.face_normals @ _AXES.T, axis=1)
    pairs = mesh.face_adjacency
    same = facing[pairs[:, 0]] == facing[pairs[:, 1]]
    pairs = pairs[same]
    count = len(mesh.faces)
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(count, count))
    _, chart = connected_components(graph, directed=False)
    return chart, facing


def _pack(sizes: np.ndarray, size: int) -> tuple[np.ndarray, float] | None:
    """Shelf-pack rectangles (w, h in texels, gutters included) into a size × size
    square. Returns each rectangle's top-left corner and the used height, or None."""
    order = np.argsort(-sizes[:, 1], kind="stable")
    corners = np.zeros_like(sizes)
    x = y = shelf = 0.0
    for index in order:
        w, h = sizes[index]
        if w > size:
            return None
        if x + w > size:
            x, y, shelf = 0.0, y + shelf, 0.0
        corners[index] = (x, y)
        x += w
        shelf = max(shelf, h)
        if y + shelf > size:
            return None
    return corners, y + shelf


def unwrap(mesh: trimesh.Trimesh, size: int) -> Unwrapped:
    """Charts by facing direction, projected flat and packed into a size × size texture
    at one texel density for the whole model."""
    chart, facing = _charts(mesh)
    count = int(chart.max()) + 1
    faces = np.asarray(mesh.faces)
    vertices = np.asarray(mesh.vertices, dtype=np.float64)

    # One unwrapped vertex per (chart, source vertex).
    corner_chart = np.repeat(chart, 3)
    keys = corner_chart.astype(np.int64) * len(vertices) + faces.reshape(-1)
    unique, inverse = np.unique(keys, return_inverse=True)
    vertex_of = unique % len(vertices)
    vertex_chart = unique // len(vertices)
    new_faces = inverse.reshape(-1, 3)

    chart_axis = np.zeros(count, dtype=np.int64)
    chart_axis[chart] = facing
    flat = np.zeros((len(unique), 2))
    for axis, direction in enumerate(_AXES):
        u, v = _basis(direction)
        rows = chart_axis[vertex_chart] == axis
        points = vertices[vertex_of[rows]]
        flat[rows, 0] = points @ u
        flat[rows, 1] = -(points @ v)  # image rows grow downwards

    low = np.full((count, 2), np.inf)
    high = np.full((count, 2), -np.inf)
    np.minimum.at(low, vertex_chart, flat)
    np.maximum.at(high, vertex_chart, flat)
    extent = high - low

    area = float(np.prod(np.maximum(extent, 1e-9), axis=1).sum())
    density = np.sqrt(0.7 * size * size / max(area, 1e-12))  # texels per model unit
    pad = 2 * GUTTER
    for _ in range(80):
        rects = np.ceil(extent * density) + pad + 1
        packed = _pack(rects, size)
        if packed is not None:
            break
        density *= 0.95
    else:  # pragma: no cover - only for absurd chart counts at tiny texture sizes
        raise ValueError(f"{count} charts do not fit in a {size} px texture")
    corners, _ = packed
    uv_pixels = (flat - low[vertex_chart]) * density + corners[vertex_chart] + GUTTER + 0.5
    used = float(np.prod(rects - pad, axis=1).sum() / (size * size))
    return Unwrapped(vertex_of, new_faces, uv_pixels, size, count, min(used, 1.0))


# --- rasterising ------------------------------------------------------------------

_SHIFT = 4  # sub-pixel bits for cv2.fillPoly


def _face_ids(triangles: np.ndarray, size: int, order: np.ndarray | None = None) -> np.ndarray:
    """An image of which face covers each pixel (-1: none). ``triangles``: (F, 3, 2)
    pixel positions. Later faces in ``order`` are drawn over earlier ones."""
    image = np.full((size, size), -1, dtype=np.int32)
    points = np.round((triangles - 0.5) * (1 << _SHIFT)).astype(np.int32)
    for index in range(len(triangles)) if order is None else order:
        cv2.fillPoly(image, [points[index]], int(index), lineType=cv2.LINE_8, shift=_SHIFT)
    return image


def _barycentric(triangles: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Barycentric weights of each point in its triangle, clamped onto the triangle."""
    a, b, c = triangles[:, 0], triangles[:, 1], triangles[:, 2]
    v0, v1, v2 = b - a, c - a, points - a
    d00 = np.einsum("ij,ij->i", v0, v0)
    d01 = np.einsum("ij,ij->i", v0, v1)
    d11 = np.einsum("ij,ij->i", v1, v1)
    d20 = np.einsum("ij,ij->i", v2, v0)
    d21 = np.einsum("ij,ij->i", v2, v1)
    denominator = d00 * d11 - d01 * d01
    denominator[np.abs(denominator) < 1e-12] = 1e-12
    w1 = (d11 * d20 - d01 * d21) / denominator
    w2 = (d00 * d21 - d01 * d20) / denominator
    weights = np.clip(np.stack([1 - w1 - w2, w1, w2], axis=1), 0.0, None)
    total = weights.sum(axis=1, keepdims=True)
    total[total == 0] = 1.0
    result: np.ndarray = weights / total
    return result


# --- camera -----------------------------------------------------------------------


def _pinhole(points: np.ndarray, size: int) -> np.ndarray:
    """TripoSR's input camera: pixel positions (x right, y down) in a size² picture."""
    depth = np.maximum(CAMERA_DISTANCE - points[:, 2], 1e-6)
    focal = 0.5 * size / np.tan(np.radians(CAMERA_FOV_DEG) / 2)
    return np.stack(
        [size / 2 + focal * points[:, 0] / depth, size / 2 - focal * points[:, 1] / depth], axis=1
    )


def _silhouette(mesh: trimesh.Trimesh, size: int) -> np.ndarray:
    # One triangle at a time: fillPoly given many polygons fills by the even-odd
    # rule, so overlapping front and back triangles would cancel out.
    triangles = _pinhole(np.asarray(mesh.vertices), size)[mesh.faces]
    return (_face_ids(triangles, size) >= 0).astype(np.uint8)


def fit_camera(mesh: trimesh.Trimesh, alpha: np.ndarray) -> CameraFit:
    """Scale and offset that line the model's outline up with the cut-out's.

    ``alpha`` is the cut-out's transparency (0..255) of the square picture the model
    was generated from.
    """
    size = FIT_SIZE
    outline = _silhouette(mesh, size).astype(np.float32)
    mask = cv2.resize(alpha, (size, size), interpolation=cv2.INTER_AREA) > 127
    centre = size / 2.0

    def score(scale: float, dx: float, dy: float) -> float:
        matrix = np.array(
            [[scale, 0, (1 - scale) * centre + dx], [0, scale, (1 - scale) * centre + dy]],
            dtype=np.float32,
        )
        moved = cv2.warpAffine(outline, matrix, (size, size), flags=cv2.INTER_NEAREST) > 0.5
        union = np.count_nonzero(moved | mask)
        return np.count_nonzero(moved & mask) / union if union else 0.0

    # Coarse to fine: each round searches around the best so far.
    best = (score(1.0, 0.0, 0.0), 1.0, 0.0, 0.0)
    for scale_steps, shift_steps in (
        (np.linspace(-0.2, 0.25, 10), np.arange(-16.0, 17.0, 4.0)),
        (np.linspace(-0.03, 0.03, 7), np.arange(-3.0, 3.1, 1.5)),
        (np.linspace(-0.01, 0.01, 5), np.arange(-1.0, 1.1, 0.5)),
    ):
        _, base_scale, base_x, base_y = best
        for step in scale_steps:
            scale = base_scale + float(step)
            for dx in base_x + shift_steps:
                for dy in base_y + shift_steps:
                    value = score(scale, float(dx), float(dy))
                    if value > best[0]:
                        best = (value, scale, float(dx), float(dy))
    iou, scale, dx, dy = best
    return CameraFit(scale=scale, offset=(dx, dy), iou=float(iou), size=size)


def _visible(mesh: trimesh.Trimesh, fit: CameraFit, points: np.ndarray) -> np.ndarray:
    """Whether the camera sees each surface point (nothing significantly in front)."""
    size = VISIBILITY_SIZE
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    pixels = fit.to_pixels(vertices, size)
    depth = CAMERA_DISTANCE - vertices[:, 2]
    order = np.argsort(-depth[faces].mean(axis=1))  # far first, near drawn last
    ids = _face_ids(pixels[faces], size, order)

    at = fit.to_pixels(points, size)
    col = np.clip(np.floor(at[:, 0]).astype(np.int64), 0, size - 1)
    row = np.clip(np.floor(at[:, 1]).astype(np.int64), 0, size - 1)
    front = ids[row, col]
    hit = front >= 0
    camera = np.array([0.0, 0.0, CAMERA_DISTANCE])
    # Where the ray from the camera to the point meets the front face's plane, as a
    # share of the way to the point: 1 means the point itself is in front.
    normals = mesh.face_normals[front[hit]]
    anchor = vertices[faces[front[hit], 0]]
    ray = points[hit] - camera
    along = np.einsum("ij,ij->i", normals, ray)
    along[np.abs(along) < 1e-12] = 1e-12
    share = np.einsum("ij,ij->i", normals, anchor - camera) / along
    seen = np.zeros(len(points), dtype=bool)
    seen[hit] = share >= 1.0 - VISIBILITY_TOLERANCE * CAMERA_DISTANCE / np.maximum(
        np.linalg.norm(ray, axis=1), 1e-9
    )
    return seen


def _sample(image: np.ndarray, pixels: np.ndarray) -> np.ndarray:
    """Bilinear samples of an image (H, W, C) at pixel positions (x right, y down)."""
    height, width = image.shape[:2]
    x = np.clip(pixels[:, 0] - 0.5, 0, width - 1)
    y = np.clip(pixels[:, 1] - 0.5, 0, height - 1)
    x0 = (
        np.minimum(np.floor(x).astype(np.int64), width - 2)
        if width > 1
        else np.zeros(len(x), np.int64)
    )
    y0 = (
        np.minimum(np.floor(y).astype(np.int64), height - 2)
        if height > 1
        else np.zeros(len(y), np.int64)
    )
    fx = (x - x0)[:, None]
    fy = (y - y0)[:, None]
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    top = image[y0, x0] * (1 - fx) + image[y0, x1] * fx
    bottom = image[y1, x0] * (1 - fx) + image[y1, x1] * fx
    sampled: np.ndarray = top * (1 - fy) + bottom * fy
    return sampled


# --- baking -----------------------------------------------------------------------


def _fill_gutters(texture: np.ndarray, filled: np.ndarray, steps: int) -> np.ndarray:
    """Grow each chart's edge colours outwards into the empty texels around it, then
    give the remaining background the average colour."""
    colour = texture.astype(np.float32) * filled[..., None]
    weight = filled.astype(np.float32)
    for _ in range(steps):
        blurred = cv2.blur(colour, (3, 3))
        count = cv2.blur(weight, (3, 3))
        grow = (weight == 0) & (count > 0)
        colour[grow] = blurred[grow] / count[grow][:, None]
        weight[grow] = 1.0
    empty = weight == 0
    if empty.any() and (~empty).any():
        colour[empty] = colour[~empty].mean(axis=0)
    result: np.ndarray = np.clip(colour, 0, 1)
    return result


def _simplified(dense: trimesh.Trimesh, triangles: int) -> tuple[trimesh.Trimesh, np.ndarray, int]:
    """The dense mesh simplified and mended (:func:`close_up`), with its normals.

    Normals come from the dense, smoothed surface (averaged by the simplifier):
    recomputing them from the simplified triangles would tilt them at slivers.
    Also returns how many crowded edges were mended.
    """
    light = trimesh.Trimesh(vertices=dense.vertices, faces=dense.faces, process=False)
    if len(dense.faces) > triangles:
        light, _ = simplify_mesh(light, triangles)
    points, faces, normals, mended = close_up(
        np.asarray(light.vertices, dtype=np.float64),
        np.asarray(light.faces, dtype=np.int64),
        np.asarray(light.vertex_normals, dtype=np.float64),
    )
    used = np.unique(faces)
    remap = np.full(len(points), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    normals = normals[used]
    light = trimesh.Trimesh(
        vertices=points[used], faces=remap[faces], vertex_normals=normals, process=False
    )
    return light, normals, mended


@dataclass(frozen=True, slots=True)
class _Texels:
    """Every texel a face covers: where it is in the texture and on the surface."""

    rows: np.ndarray
    cols: np.ndarray
    points: np.ndarray
    normals: np.ndarray


def _texels(light: trimesh.Trimesh, normals: np.ndarray, flat: Unwrapped) -> _Texels:
    ids = _face_ids(flat.uv_pixels[flat.faces], flat.size)
    rows, cols = np.nonzero(ids >= 0)
    face = ids[rows, cols]
    centres = np.stack([cols + 0.5, rows + 0.5], axis=1)
    weights = _barycentric(flat.uv_pixels[flat.faces[face]], centres)
    corners = flat.vertex_of[flat.faces[face]]  # (N, 3) vertices of the light mesh
    points = np.einsum("ij,ijk->ik", weights, np.asarray(light.vertices)[corners])
    point_normals = np.einsum("ij,ijk->ik", weights, normals[corners])
    point_normals /= np.maximum(np.linalg.norm(point_normals, axis=1, keepdims=True), 1e-12)
    return _Texels(rows, cols, points, point_normals)


def _dense_colours(dense: trimesh.Trimesh, points: np.ndarray) -> np.ndarray:
    """The dense mesh's colour at each point: inverse-distance mean of the 4 nearest
    vertices (sRGB 0..1)."""
    colours = _vertex_colours_srgb(dense)
    distance, nearest = cKDTree(np.asarray(dense.vertices)).query(points, k=4)
    inverse = 1.0 / np.maximum(distance, 1e-6)
    mean: np.ndarray = (
        np.einsum("ij,ijk->ik", inverse, colours[nearest]) / inverse.sum(axis=1)[:, None]
    )
    return mean


def _with_photo(
    light: trimesh.Trimesh,
    photo: np.ndarray,
    texels: _Texels,
    colours: np.ndarray,
    report: RefineReport,
) -> np.ndarray:
    """``colours`` with the photo projected on where the camera saw the surface."""
    picture = photo
    if max(picture.shape[:2]) > PHOTO_MAX_SIZE:
        picture = cv2.resize(
            picture, (PHOTO_MAX_SIZE, PHOTO_MAX_SIZE), interpolation=cv2.INTER_AREA
        )
    fit = fit_camera(light, picture[..., 3])
    report.photo_fit_iou = round(fit.iou, 3)
    if fit.iou < MIN_FIT_IOU:
        report.note = (
            "The model's outline did not line up with the cut-out "
            f"({fit.iou:.0%} overlap), so the photo was not projected onto it."
        )
        return colours
    points = texels.points
    seen = _visible(light, fit, points)
    towards = np.array([0.0, 0.0, CAMERA_DISTANCE]) - points
    towards /= np.maximum(np.linalg.norm(towards, axis=1, keepdims=True), 1e-12)
    facing = np.einsum("ij,ij->i", texels.normals, towards)
    low, high = np.cos(np.radians(PHOTO_NONE_DEG)), np.cos(np.radians(PHOTO_FULL_DEG))
    ramp = np.clip((facing - low) / (high - low), 0.0, 1.0)
    ramp = ramp * ramp * (3 - 2 * ramp)
    sampled = _sample(picture.astype(np.float32) / 255.0, fit.to_pixels(points, picture.shape[0]))
    strength = ramp * seen * sampled[:, 3]
    report.photo_projected = True
    report.photo_share = round(float((strength > 0.5).mean()), 3)
    blended: np.ndarray = colours * (1 - strength[:, None]) + sampled[:, :3] * strength[:, None]
    return blended


def _textured(
    light: trimesh.Trimesh,
    normals: np.ndarray,
    flat: Unwrapped,
    texels: _Texels,
    colours: np.ndarray,
) -> trimesh.Trimesh:
    """The unwrapped mesh with the colours as its base-colour texture."""
    size = flat.size
    texture = np.zeros((size, size, 3), dtype=np.float32)
    texture[texels.rows, texels.cols] = colours
    filled = np.zeros((size, size), dtype=bool)
    filled[texels.rows, texels.cols] = True
    texture = _fill_gutters(texture, filled, GUTTER + 2)
    image = Image.fromarray(np.round(texture * 255).astype(np.uint8), mode="RGB")
    uv = np.stack([flat.uv_pixels[:, 0] / size, 1.0 - flat.uv_pixels[:, 1] / size], axis=1)
    material = trimesh.visual.material.PBRMaterial(  # type: ignore[no-untyped-call]
        name="baked", baseColorTexture=image, metallicFactor=0.0, roughnessFactor=ROUGHNESS
    )
    return trimesh.Trimesh(
        vertices=np.asarray(light.vertices)[flat.vertex_of],
        faces=flat.faces,
        vertex_normals=normals[flat.vertex_of],
        visual=trimesh.visual.TextureVisuals(uv=uv, material=material),  # type: ignore[no-untyped-call]
        process=False,
    )


def refine(
    mesh: trimesh.Trimesh,
    settings: RefineSettings,
    photo: np.ndarray | None = None,
    on_step: Callable[[str], None] | None = None,
) -> tuple[trimesh.Trimesh, RefineReport]:
    """Clean up, simplify, unwrap and texture a generated model.

    ``mesh`` has linear per-vertex colours (as TripoSR's GLB stores them).
    ``photo``: the square RGBA picture the model was generated from (cut-out on
    transparency), or None to bake from the vertex colours only.
    ``on_step`` is called with each finished step's name (the job worker uses it to
    show it is still alive).
    """
    report = RefineReport(triangles_before=len(mesh.faces), texture_size=settings.texture_size)
    clock = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal clock
        now = time.perf_counter()
        report.seconds[name] = round(now - clock, 2)
        clock = now
        if on_step is not None:
            on_step(name)

    mesh, report.fragments_removed, report.fragment_area_share = remove_fragments(mesh)
    dense = smooth(mesh, settings.smooth_iterations)
    lap("cleaning")
    light, normals, report.edges_closed = _simplified(dense, settings.triangles)
    lap("simplifying")
    flat = unwrap(light, settings.texture_size)
    report.charts, report.texture_fill = flat.charts, round(flat.fill, 3)
    lap("unwrapping")
    texels = _texels(light, normals, flat)
    colours = _dense_colours(dense, texels.points)
    lap("baking")
    if photo is not None:
        colours = _with_photo(light, photo, texels, colours, report)
        lap("projecting")
    result = _textured(light, normals, flat, texels, colours)
    report.triangles_after = len(result.faces)
    lap("texturing")
    return result, report
