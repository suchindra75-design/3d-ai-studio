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
    photo_share: float = 0.0  # share of the surface coloured (mostly) from the photos
    views: list[dict[str, Any]] = field(default_factory=list)  # the other photos
    depth: dict[str, Any] | None = None  # the depth correction from side photos
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


def fit_camera(mesh: trimesh.Trimesh, alpha: np.ndarray, *, cut_below: bool = False) -> CameraFit:
    """Scale and offset that line the model's outline up with the cut-out's.

    ``alpha`` is the cut-out's transparency (0..255) of the square picture the model
    was generated from. With ``cut_below`` only the rows down to the cut-out's lowest
    are compared: another photo, cut off by its frame higher or lower than the front
    photo, says nothing about the model below that.
    """
    size = FIT_SIZE
    outline = _silhouette(mesh, size).astype(np.float32)
    mask = cv2.resize(alpha, (size, size), interpolation=cv2.INTER_AREA) > 127
    rows = slice(None)
    if cut_below and mask.any():
        rows = slice(0, int(np.nonzero(mask.any(axis=1))[0].max()) + 1)
    mask = mask[rows]
    centre = size / 2.0

    def score(scale: float, dx: float, dy: float) -> float:
        matrix = np.array(
            [[scale, 0, (1 - scale) * centre + dx], [0, scale, (1 - scale) * centre + dy]],
            dtype=np.float32,
        )
        moved = cv2.warpAffine(outline, matrix, (size, size), flags=cv2.INTER_NEAREST)[rows] > 0.5
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


# --- other views -----------------------------------------------------------------
#
# With photos of the other sides (left, back, right, as a person turning in front of
# the camera or someone walking around the object takes them), the front model is
# corrected and coloured from them: TripoSR made the shape from the front photo
# only, so its depth is a guess. The side photos' outlines give the real depth at
# each height; the photos themselves colour the sides they saw.

# Where each view's camera is, from the model's centre (glTF axes: front +Z, up +Y;
# +X is the model's own left, which the front camera sees on the right).
VIEW_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "left": (1.0, 0.0, 0.0),
    "back": (0.0, 0.0, -1.0),
    "right": (-1.0, 0.0, 0.0),
}
DEPTH_BINS = 96
# Side photos are matched at sizes within this range of the front photo's (photos
# taken from about the same distance). The upper half of the outline decides (head,
# neck and shoulders, whose heights TripoSR gets from the front photo); lower down
# its guessed depth can be far off, which is what is being corrected.
DEPTH_SCALES = np.linspace(0.8, 1.25, 19)
DEPTH_MATCH_PART = 0.5  # the top share of the height used to line the photos up
DEPTH_MIN_CORRELATION = 0.5  # how well that part of the side outline must match
DEPTH_LIMITS = (0.5, 3.0)  # how far the depth may be stretched or squeezed
# The correction changes gradually with height (over about a tenth of it): where
# TripoSR's parts end (arms beside a torso) a sharper change pulls them into flaps.
DEPTH_SMOOTH_BINS = 11


@dataclass(frozen=True, slots=True)
class ViewPhoto:
    """Another photo of the object: its square cut-out (framed as TripoSR frames the
    front photo) and the side of that square in pixels of the photo as taken."""

    role: str  # "left", "back" or "right"
    picture: np.ndarray  # RGBA
    crop_side: float


def turn_towards(direction: np.ndarray) -> np.ndarray:
    """The rotation about the vertical axis that brings ``direction`` to the front
    (+Z): applied to the model, a camera there sees what the front camera sees."""
    angle = np.arctan2(direction[0], direction[2])
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, 0.0, -s], [0.0, 1.0, 0.0], [s, 0.0, c]])


def _units_per_pixel(fit: CameraFit, crop_side: float) -> float:
    """Model units per photo pixel at the model's centre, for the front fit."""
    focal = 0.5 / np.tan(np.radians(CAMERA_FOV_DEG) / 2)  # per unit of picture size
    return float(CAMERA_DISTANCE / (focal * fit.scale * crop_side))


def _smooth_profile(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Fill gaps by interpolation, then a median and a triangular mean along the
    height, each DEPTH_SMOOTH_BINS long."""
    index = np.arange(len(values))
    filled = np.interp(index, index[valid], values[valid])
    half = DEPTH_SMOOTH_BINS // 2
    windows = np.lib.stride_tricks.sliding_window_view(
        np.pad(filled, half, mode="edge"), 2 * half + 1
    )
    median = np.median(windows, axis=1)
    kernel = np.concatenate([np.arange(1, half + 2), np.arange(half, 0, -1)]).astype(np.float64)
    kernel /= kernel.sum()
    result: np.ndarray = np.convolve(np.pad(median, half, mode="edge"), kernel, mode="valid")
    return result


# The model's depth in a band is measured on its core: the middle CORE_SHARE of the
# band from side to side (head, neck, torso), from the bulk of its surface. Parts
# beside it (arms) start and end at other heights; measured with them, the depth
# would jump where they end and the correction would shear the surface there.
MODEL_EXTENT_PERCENTILES = (0.5, 99.5)
CORE_SHARE = 0.5
SIDE_STRETCH = 0.0  # share of the stretch the outermost parts (arms) get
OUTLINE_MARGIN = 0.02


def _model_profile(
    across: np.ndarray,
    height: np.ndarray,
    sideways: np.ndarray,
    edges: np.ndarray,
    *,
    core: bool = True,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per height band: where the bulk of the core's surface (or, with ``core``
    False, of all of it) starts and ends along ``across``, and whether the band has
    any surface."""
    count = len(edges) - 1
    band = np.clip(np.searchsorted(edges, height) - 1, 0, count - 1)
    order = np.argsort(band, kind="stable")
    starts = np.searchsorted(band[order], np.arange(count + 1))
    low = np.full(count, np.inf)
    high = np.full(count, -np.inf)
    for k in range(count):
        members = order[starts[k] : starts[k + 1]]
        if len(members) < 3:
            continue
        if core:
            left, right = np.percentile(sideways[members], MODEL_EXTENT_PERCENTILES)
            middle, half = 0.5 * (left + right), 0.5 * CORE_SHARE * (right - left)
            members = members[np.abs(sideways[members] - middle) <= half]
        if len(members) >= 3:
            low[k], high[k] = np.percentile(across[members], MODEL_EXTENT_PERCENTILES)
    return low, high, np.isfinite(low)


def _photo_rows(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per picture row: the outline's left and right column, and whether it has one."""
    rows = np.asarray(mask.any(axis=1), dtype=bool)
    left = np.where(rows, mask.argmax(axis=1), 0).astype(np.float64)
    right = np.where(rows, mask.shape[1] - 1 - mask[:, ::-1].argmax(axis=1), 0).astype(np.float64)
    return left, right + 1.0, rows


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation, NaN when either does not vary."""
    a, b = a - a.mean(), b - b.mean()
    spread = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / spread) if spread > 1e-12 else float("nan")


@dataclass(frozen=True, slots=True)
class _Outline:
    """A side photo's outline, per picture row (columns), and where its top is."""

    left: np.ndarray
    right: np.ndarray
    rows: np.ndarray
    top_row: int


def _match(
    model: tuple[np.ndarray, np.ndarray, np.ndarray],
    outline: _Outline,
    edges: np.ndarray,
    unit: float,
) -> dict[str, Any] | None:
    """How well the photo's outline, at ``unit`` model units per pixel and with its
    top at the model's top, matches the model's per-band depth profile."""
    m_low, m_high, m_valid = model
    centres = 0.5 * (edges[:-1] + edges[1:])
    top, bottom = edges[-1], edges[0]
    size = len(outline.rows)
    row_height = top - (np.arange(size) + 0.5 - outline.top_row) * unit
    band = np.searchsorted(edges, row_height) - 1
    inside = outline.rows & (band >= 0) & (band < DEPTH_BINS)
    if inside.sum() < 20:
        return None
    p_low = np.full(DEPTH_BINS, np.inf)
    p_high = np.full(DEPTH_BINS, -np.inf)
    np.minimum.at(p_low, band[inside], outline.left[inside])
    np.maximum.at(p_high, band[inside], outline.right[inside])
    valid = m_valid & np.isfinite(p_low)
    upper = valid & (centres >= top - DEPTH_MATCH_PART * (top - bottom))
    if valid.sum() < DEPTH_BINS // 4 or upper.sum() < 8:
        return None
    m_width, m_centre = m_high - m_low, 0.5 * (m_high + m_low)
    p_width = np.where(valid, p_high - p_low, 0.0) * unit
    p_centre = np.where(valid, 0.5 * (p_high + p_low), 0.0) * unit
    score = _correlation(m_width[upper], p_width[upper])
    # The profile's shape (face, back of the head) decides which way it faces; a
    # profile that does not lean either way (a symmetric object) says nothing.
    lean = _correlation(m_centre[upper], p_centre[upper])
    lean = 0.0 if np.isnan(lean) else lean
    return {
        "total": score + 0.5 * lean,
        "score": score,
        "valid": valid,
        "m_width": m_width,
        "m_centre": m_centre,
        "p_width": p_width,
        "p_centre": p_centre,
    }


def _side_correction(
    vertices: np.ndarray,
    view: ViewPhoto,
    units_per_pixel: float,
) -> dict[str, Any]:
    """How one side photo says the depth should be, per height band.

    Returns the band edges and, per band, ``offset`` and ``factor`` such that the
    corrected depth is ``offset + factor × z``, plus what was found.
    """
    direction = np.array(VIEW_DIRECTIONS[view.role])
    mask = view.picture[..., 3] > 127
    left, right, rows = _photo_rows(mask)
    if rows.sum() < 20:
        return {"role": view.role, "used": False, "note": "No object in this photo."}
    outline = _Outline(left, right, rows, int(np.argmax(rows)))
    top, bottom = float(vertices[:, 1].max()), float(vertices[:, 1].min())
    edges = np.linspace(bottom, top, DEPTH_BINS + 1)
    centres = 0.5 * (edges[:-1] + edges[1:])

    best: dict[str, Any] | None = None
    for flipped in (False, True):
        facing = -direction if flipped else direction
        across = (vertices @ turn_towards(facing).T)[:, 0]  # the photo's left → right
        model = _model_profile(across, vertices[:, 1], vertices[:, 0], edges)
        for scale in DEPTH_SCALES:
            unit = units_per_pixel * view.crop_side / mask.shape[0] * scale
            found = _match(model, outline, edges, unit)
            if found is None or not np.isfinite(found["total"]):
                continue
            if best is None or found["total"] > best["total"]:
                best = found | {"scale": float(scale), "flipped": flipped}
    if best is None or not np.isfinite(best["score"]) or best["score"] < DEPTH_MIN_CORRELATION:
        found = None if best is None else round(best["score"], 2)
        return {
            "role": view.role,
            "used": False,
            "note": f"The side outline did not match the model's (match {found}).",
        }
    valid = best["valid"]
    # Bands without surface hold no numbers (inf, NaN); only valid bands are used.
    m_width = np.where(valid, best["m_width"], 1.0)
    m_centre = np.where(valid, best["m_centre"], 0.0)
    factor = np.clip(best["p_width"] / np.maximum(m_width, 1e-6), *DEPTH_LIMITS)
    weight = np.where(valid, m_width, 0.0)
    shift = np.average(m_centre, weights=weight) - np.average(best["p_centre"], weights=weight)
    target = best["p_centre"] + shift  # where the photo puts each band's middle
    half_width = _smooth_profile(0.5 * best["p_width"], valid)
    factor = _smooth_profile(factor, valid)
    target = _smooth_profile(target, valid)
    model_centre = _smooth_profile(m_centre, valid)
    # across' = target + (across - model_centre) × factor, and across = -sign × z, so
    # z' = z_target + (z - z_model) × factor.
    sign = (-direction if best["flipped"] else direction)[0]
    # The photo's outline as limits on z (nothing may stick out beyond it).
    ends = np.stack([-sign * (target - half_width), -sign * (target + half_width)])
    return {
        "role": view.role,
        "used": True,
        "flipped": bool(best["flipped"]),
        "match": round(best["score"], 3),
        "scale": round(best["scale"], 3),
        "edges": edges,
        "centres": centres,
        "z_target": -sign * target,
        "z_model": -sign * model_centre,
        "factor": factor,
        "z_low": ends.min(axis=0),
        "z_high": ends.max(axis=0),
        "valid": valid,
    }


def correct_depth(
    mesh: trimesh.Trimesh,
    front: CameraFit,
    front_crop_side: float,
    views: list[ViewPhoto],
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """Stretch or squeeze the model front to back, height band by height band, so
    its side outline matches the side photos' (``left``/``right`` views)."""
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    unit = _units_per_pixel(front, front_crop_side)
    found = [
        _side_correction(vertices, view, unit) for view in views if view.role in ("left", "right")
    ]
    used = [f for f in found if f["used"]]
    summary: dict[str, Any] = {
        "applied": False,
        "views": [
            {
                k: v
                for k, v in f.items()
                if k in ("role", "used", "flipped", "match", "scale", "note")
            }
            for f in found
        ],
    }
    if not used:
        return mesh, summary
    centres, edges = used[0]["centres"], used[0]["edges"]
    z_target = np.mean([f["z_target"] for f in used], axis=0)
    z_model = np.mean([f["z_model"] for f in used], axis=0)
    factor = np.mean([f["factor"] for f in used], axis=0)
    height = vertices[:, 1]
    # Full correction on the core (head, neck, torso); parts further out to the sides
    # (arms) get a gentler stretch, so their ends are not pulled into fins.
    low, high, valid = _model_profile(vertices[:, 0], height, vertices[:, 0], edges, core=False)
    middle = _smooth_profile(np.where(valid, 0.5 * (low + high), 0.0), valid)
    half = _smooth_profile(np.where(valid, 0.5 * (high - low), 1.0), valid)
    out = np.abs(vertices[:, 0] - np.interp(height, centres, middle)) / np.maximum(
        np.interp(height, centres, half), 1e-6
    )
    # 1 on the core, easing (smoothstep, no crease) to SIDE_STRETCH at the outermost.
    ease = np.clip((out - CORE_SHARE) / (1.0 - CORE_SHARE), 0.0, 1.0)
    ease = ease * ease * (3.0 - 2.0 * ease)
    keep = 1.0 - ease * (1.0 - SIDE_STRETCH)
    stretch = 1.0 + (np.interp(height, centres, factor) - 1.0) * keep
    corrected = vertices.copy()
    corrected[:, 2] = (
        np.interp(height, centres, z_target)
        + (vertices[:, 2] - np.interp(height, centres, z_model)) * stretch
    )
    # Within the photos' outline: what sticks out by more than OUTLINE_MARGIN (of the
    # height) is pressed back to that margin (fins, ledges).
    margin = OUTLINE_MARGIN * float(np.ptp(height))
    z_low = np.mean([f["z_low"] for f in used], axis=0) - margin
    z_high = np.mean([f["z_high"] for f in used], axis=0) + margin
    corrected[:, 2] = np.clip(
        corrected[:, 2], np.interp(height, centres, z_low), np.interp(height, centres, z_high)
    )
    result = mesh.copy()
    result.vertices = corrected
    depth_before = float(np.ptp(vertices[:, 2]))
    summary.update(
        applied=True,
        depth_before=round(depth_before, 4),
        depth_after=round(float(np.ptp(corrected[:, 2])), 4),
        factor_range=[round(float(factor.min()), 2), round(float(factor.max()), 2)],
    )
    return result, summary


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


def _fit_picture(photo: np.ndarray) -> np.ndarray:
    if max(photo.shape[:2]) <= PHOTO_MAX_SIZE:
        return photo
    resized: np.ndarray = cv2.resize(
        photo, (PHOTO_MAX_SIZE, PHOTO_MAX_SIZE), interpolation=cv2.INTER_AREA
    )
    return resized


# Where the other photos overlap, each texel mostly takes the one that faces it most
# directly (weights grow with the facing cosine to this power).
BLEND_POWER = 8.0


# A photo's outermost pixels mix in the background (a cut-out's edge is soft), so
# colours are taken from inside the cut-out: its edge is pulled in by this share of
# the picture size and the strength rises gradually over the same distance.
EDGE_INSET = 0.012


def _inner_alpha(alpha: np.ndarray) -> np.ndarray:
    """0..1: how far inside the cut-out each pixel is, reaching 1 at 2 × EDGE_INSET."""
    inside = (alpha > 127).astype(np.uint8)
    distance = cv2.distanceTransform(inside, cv2.DIST_L2, 5) / alpha.shape[0]
    inner: np.ndarray = np.clip((distance - EDGE_INSET) / EDGE_INSET, 0.0, 1.0).astype(np.float32)
    return inner


@dataclass(frozen=True, slots=True)
class _Layer:
    """One photo's colours at the texels, how strongly each texel takes them, and how
    directly each texel faces that photo's camera (cosine)."""

    colours: np.ndarray
    strength: np.ndarray
    facing: np.ndarray


def _photo_layer(
    light: trimesh.Trimesh,
    photo: np.ndarray,
    texels: _Texels,
    turn: np.ndarray | None = None,
) -> tuple[_Layer | None, CameraFit]:
    """The photo projected onto what its camera saw. ``turn`` brings that camera's
    side of the model to the front (None: the front photo)."""
    picture = _fit_picture(photo)
    mesh, points, normals = light, texels.points, texels.normals
    colour_alpha = _inner_alpha(picture[..., 3])
    if turn is not None:
        mesh = trimesh.Trimesh(
            vertices=np.asarray(light.vertices) @ turn.T, faces=light.faces, process=False
        )
        points, normals = points @ turn.T, normals @ turn.T
    fit = fit_camera(mesh, picture[..., 3], cut_below=turn is not None)
    if fit.iou < MIN_FIT_IOU:
        return None, fit
    seen = _visible(mesh, fit, points)
    towards = np.array([0.0, 0.0, CAMERA_DISTANCE]) - points
    towards /= np.maximum(np.linalg.norm(towards, axis=1, keepdims=True), 1e-12)
    facing = np.einsum("ij,ij->i", normals, towards)
    low, high = np.cos(np.radians(PHOTO_NONE_DEG)), np.cos(np.radians(PHOTO_FULL_DEG))
    ramp = np.clip((facing - low) / (high - low), 0.0, 1.0)
    ramp = ramp * ramp * (3 - 2 * ramp)
    sampled = _sample(picture.astype(np.float32) / 255.0, fit.to_pixels(points, picture.shape[0]))
    inner = _sample(colour_alpha[..., None], fit.to_pixels(points, picture.shape[0]))[:, 0]
    layer = _Layer(sampled[:, :3], ramp * seen * inner, np.clip(facing, 0.0, 1.0))
    return layer, fit


# Photos taken from other sides are often lit differently (a window on one side).
# Where a photo and the front photo both see the surface and agree on its hue, its
# brightness is scaled to match the front photo's, within these limits. Where they
# disagree on hue they do not show the same thing there; nothing is changed.
MATCH_MIN_TEXELS = 500
MATCH_GAIN_LIMITS = (0.6, 1.6)
MATCH_MIN_HUE_COSINE = 0.98


def _matched(layer: _Layer, reference: _Layer) -> _Layer:
    """``layer`` with its brightness matched to ``reference`` where both are strong."""
    both = (layer.strength > 0.3) & (reference.strength > 0.3)
    if both.sum() < MATCH_MIN_TEXELS:
        return layer
    ours = layer.colours[both].mean(axis=0)
    theirs = reference.colours[both].mean(axis=0)
    norms = float(np.linalg.norm(ours) * np.linalg.norm(theirs))
    if norms < 1e-6 or float(ours @ theirs) / norms < MATCH_MIN_HUE_COSINE:
        return layer
    gain = float(np.clip(np.linalg.norm(theirs) / np.linalg.norm(ours), *MATCH_GAIN_LIMITS))
    return _Layer(np.clip(layer.colours * gain, 0.0, 1.0), layer.strength, layer.facing)


def _blend(colours: np.ndarray, layers: list[_Layer]) -> np.ndarray:
    """The photos over the model's own colours. The front photo (first, made the
    shape and lines up best) comes first; the other photos only fill what it does
    not cover, the one facing the surface most directly dominating (BLEND_POWER).
    Where their total strength is under 1, the model's own colour fills the rest."""
    if not layers:
        return colours
    first, others = layers[0], [_matched(layer, layers[0]) for layer in layers[1:]]
    result = colours * (1 - first.strength[:, None]) + first.colours * first.strength[:, None]
    if not others:
        only: np.ndarray = result
        return only
    room = 1.0 - first.strength
    strength = np.stack([layer.strength for layer in others], axis=1)
    weight = strength * np.stack([layer.facing**BLEND_POWER for layer in others], axis=1)
    mixed = np.einsum("ij,ijk->ik", weight, np.stack([layer.colours for layer in others], axis=1))
    mixed /= np.maximum(weight.sum(axis=1), 1e-12)[:, None]
    cover = (np.clip(strength.sum(axis=1), 0.0, 1.0) * room)[:, None]
    blended: np.ndarray = result * (1 - cover) + mixed * cover
    return blended


def _project_photos(
    light: trimesh.Trimesh,
    photo: np.ndarray,
    views: list[ViewPhoto],
    texels: _Texels,
    colours: np.ndarray,
    *,
    flipped: dict[str, bool],
    report: RefineReport,
) -> np.ndarray:
    """The front photo and the other views' photos, projected and blended."""
    layers: list[_Layer] = []
    front, fit = _photo_layer(light, photo, texels)
    report.photo_fit_iou = round(fit.iou, 3)
    if front is None:
        report.note = (
            "The model's outline did not line up with the cut-out "
            f"({fit.iou:.0%} overlap), so the photo was not projected onto it."
        )
    else:
        layers.append(front)
        report.photo_projected = True
    for view in views:
        direction = np.array(VIEW_DIRECTIONS[view.role])
        if flipped.get(view.role):
            direction = -direction
        layer, view_fit = _photo_layer(light, view.picture, texels, turn_towards(direction))
        entry: dict[str, Any] = {"role": view.role, "fit_iou": round(view_fit.iou, 3)}
        if layer is None:
            entry.update(used=False, note="Its outline did not line up with the model's.")
        else:
            entry.update(used=True, share=round(float((layer.strength > 0.5).mean()), 3))
            layers.append(layer)
        report.views.append(entry)
    if layers:
        total = np.sum([layer.strength for layer in layers], axis=0)
        report.photo_share = round(float((total > 0.5).mean()), 3)
    return _blend(colours, layers)


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
    *,
    views: list[ViewPhoto] | None = None,
    photo_crop_side: float | None = None,
) -> tuple[trimesh.Trimesh, RefineReport]:
    """Clean up, simplify, unwrap and texture a generated model.

    ``mesh`` has linear per-vertex colours (as TripoSR's GLB stores them).
    ``photo``: the square RGBA picture the model was generated from (cut-out on
    transparency), or None to bake from the vertex colours only.
    ``views``: photos of the other sides; with ``photo_crop_side`` (the front
    square's side in pixels of the photo as taken) the side views also correct the
    depth. ``on_step`` is called with each finished step's name (the job worker uses
    it to show it is still alive).
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

    views = list(views or ())
    mesh, report.fragments_removed, report.fragment_area_share = remove_fragments(mesh)
    flipped: dict[str, bool] = {}
    if photo is not None and photo_crop_side and any(v.role in ("left", "right") for v in views):
        front = fit_camera(mesh, _fit_picture(photo)[..., 3])
        if front.iou >= MIN_FIT_IOU:
            mesh, report.depth = correct_depth(mesh, front, photo_crop_side, views)
            flipped = {v["role"]: bool(v.get("flipped")) for v in report.depth["views"]}
        else:
            report.depth = {"applied": False, "note": "The front photo did not line up."}
        lap("depth")
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
        colours = _project_photos(
            light, photo, views, texels, colours, flipped=flipped, report=report
        )
        lap("projecting")
    result = _textured(light, normals, flat, texels, colours)
    report.triangles_after = len(result.faces)
    lap("texturing")
    return result, report
