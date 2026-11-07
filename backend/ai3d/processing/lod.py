"""Levels of detail: simplified copies of a large model for viewing (D58).

Each level keeps the model's parts, node transforms, materials and texture
coordinates; only the triangle count changes. The original file is never
touched: levels are extra files the viewer draws when the full detail would not
be visible anyway (far away, or many triangles per screen pixel).

Simplification is quadric edge collapse (``fast-simplification``) with open
borders kept. Texture seams are borders (glTF splits vertices there), so UV
islands keep their outlines. A simplified vertex takes the average UV, colour
and normal of the original vertices that collapsed into it; those all lie in
one island, so the average stays on it.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import fast_simplification
import numpy as np
import trimesh
from PIL import Image

from ai3d.processing.mesh import GlbCounts

log = logging.getLogger(__name__)

# Below this many triangles a model draws fast enough as it is.
LOD_MIN_TRIANGLES = 500_000
# Each level keeps this share of the previous one's triangles…
LOD_STEP = 0.25
# …until a level would fall under this (coarser levels add little).
LOD_MIN_LEVEL_TRIANGLES = 30_000
# Parts this small are copied unchanged.
LOD_MIN_PART_TRIANGLES = 1_000
# Textures in level files are only shown until the full model has loaded.
LOD_TEXTURE_MAX = 1024
# A level is dropped (and no coarser one made) if simplification broke the shape:
# the model's bounds moved by more than 1% of its diagonal, or its vertices by more
# than 3% on average. (Vertices always move some way when many collapse into one,
# more so on coarse levels; a broken result moves them by tens of percent.) Very
# uneven meshes, e.g. hundreds of triangles meeting at one vertex, can make the
# simplifier fail like that.
LOD_MAX_BOUNDS_CHANGE = 0.01
LOD_MAX_MEAN_SHIFT = 0.03
# A level that keeps more than this share of the triangles it was made from is
# dropped (and no coarser one made): it would draw almost as much, for more memory.
LOD_MIN_REDUCTION = 0.9
# Peak memory of building the levels, simplifying in pieces, measured
# (docs/BENCHMARKS.md): 1.18 GB for 1 million triangles, 2.9 GB for 5 million, 3.6
# GB for 10 million and 4.1 GB for 20 million (the pieces keep the simplifier's
# share level), and more when vertices are not shared (4.2 GB for a 5-million-
# triangle soup with 15 million vertices). The estimate (memory_bytes) is at least
# 5% above every run (tests/test_memory_estimates.py).
LOD_BASE_BYTES = 1024 * 1024 * 1024
LOD_BYTES_PER_PIECE_TRIANGLE = 450  # up to LOD_PIECE_TRIANGLES (the simplifier)
LOD_BYTES_PER_TRIANGLE = 120  # every triangle (the whole mesh, loaded)
LOD_BYTES_PER_VERTEX = 130


def memory_bytes(counts: GlbCounts) -> int:
    """Estimated peak memory of building the levels of a model, textures included."""
    return (
        LOD_BASE_BYTES
        + min(counts.triangles, LOD_PIECE_TRIANGLES) * LOD_BYTES_PER_PIECE_TRIANGLE
        + counts.triangles * LOD_BYTES_PER_TRIANGLE
        + counts.vertices * LOD_BYTES_PER_VERTEX
        + counts.texture_bytes
    )


@dataclass(frozen=True, slots=True)
class LodLevel:
    level: int  # 1 = first simplification
    triangles: int
    # How far the original vertices are from their simplified counterparts, as a
    # share of the model's diagonal (mean and largest).
    mean_shift: float
    max_shift: float
    seconds: float
    glb: bytes


def level_targets(triangles: int) -> list[int]:
    """Triangle budgets for each level (empty when the model is small enough)."""
    if triangles < LOD_MIN_TRIANGLES:
        return []
    targets: list[int] = []
    current = float(triangles)
    while current * LOD_STEP >= LOD_MIN_LEVEL_TRIANGLES:
        current *= LOD_STEP
        targets.append(int(current))
    return targets


def _average(values: np.ndarray, mapping: np.ndarray, count: int) -> np.ndarray:
    """Mean of ``values`` rows grouped by ``mapping`` (rows mapped to -1 ignored)."""
    keep = mapping >= 0
    groups = mapping[keep]
    rows = values[keep].astype(np.float64)
    sizes = np.bincount(groups, minlength=count).astype(np.float64)
    sizes[sizes == 0] = 1.0
    columns = [
        np.bincount(groups, weights=rows[:, c], minlength=count) for c in range(rows.shape[1])
    ]
    return np.stack(columns, axis=1) / sizes[:, None]


def _small_material(material: object) -> object:
    """A copy of the material with only a downscaled base-colour texture."""
    if isinstance(material, trimesh.visual.material.SimpleMaterial):
        material = material.to_pbr()  # type: ignore[no-untyped-call]
    if not isinstance(material, trimesh.visual.material.PBRMaterial):
        return material
    image = material.baseColorTexture
    if isinstance(image, Image.Image) and max(image.size) > LOD_TEXTURE_MAX:
        factor = LOD_TEXTURE_MAX / max(image.size)
        image = image.resize(
            (max(1, round(image.width * factor)), max(1, round(image.height * factor))),
            Image.Resampling.LANCZOS,
        )
    return trimesh.visual.material.PBRMaterial(  # type: ignore[no-untyped-call]
        name=material.name,
        baseColorFactor=material.baseColorFactor,
        baseColorTexture=image,
        metallicFactor=material.metallicFactor,
        roughnessFactor=material.roughnessFactor,
        alphaMode=material.alphaMode,
        alphaCutoff=material.alphaCutoff,
        doubleSided=material.doubleSided,
    )


def _colours(mesh: trimesh.Trimesh) -> np.ndarray | None:
    visual = mesh.visual
    if isinstance(visual, trimesh.visual.ColorVisuals):
        return np.asarray(visual.vertex_colors) if visual.kind == "vertex" else None
    colour = getattr(visual, "vertex_attributes", {}).get("color")
    return None if colour is None else np.asarray(colour)


_CHUNK = 1_000_000


def _vertex_normals(mesh: trimesh.Trimesh) -> np.ndarray:
    """The file's normals when it has them; otherwise face normals weighted by each
    corner's angle and summed per vertex, in chunks (trimesh's method, without its
    cached intermediates, which held about 2 GB on 10 million triangles)."""
    cached = mesh._cache.cache.get("vertex_normals")
    if cached is not None and len(cached) == len(mesh.vertices):
        return np.asarray(cached)
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    total = np.zeros_like(vertices)
    for start in range(0, len(faces), _CHUNK):
        part = faces[start : start + _CHUNK]
        corners = vertices[part]
        cross = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
        length = np.linalg.norm(cross, axis=1)
        unit = cross / np.where(length == 0, 1.0, length)[:, None]
        for k in range(3):
            a = corners[:, (k + 1) % 3] - corners[:, k]
            b = corners[:, (k + 2) % 3] - corners[:, k]
            cosine = np.einsum("ij,ij->i", a, b) / np.maximum(
                np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1), 1e-300
            )
            angle = np.arccos(np.clip(cosine, -1.0, 1.0))
            for axis in range(3):
                total[:, axis] += np.bincount(
                    part[:, k], weights=unit[:, axis] * angle, minlength=len(vertices)
                )
    lengths = np.linalg.norm(total, axis=1)
    lengths[lengths == 0] = 1.0
    normals: np.ndarray = total / lengths[:, None]
    return normals


# Larger meshes are simplified in pieces (slabs along their longest side): the
# simplifier's own memory grows with the mesh it is given (about 3.7 GB beyond the
# model for 10 million triangles at once). Each piece keeps its cut edges exactly
# (borders are preserved), so the pieces meet without gaps; the cuts keep their full
# density.
LOD_PIECE_TRIANGLES = 2_500_000


def _collapse(
    vertices: np.ndarray, faces: np.ndarray, target: int
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Simplify, in pieces if large. Per piece: the vertices it uses (indices into
    ``vertices``), its simplified points and triangles, and for each used vertex the
    index of its simplified point (-1 if unused)."""
    count = max(1, -(-len(faces) // LOD_PIECE_TRIANGLES))
    if count == 1:
        groups = [faces]
    else:
        axis = int(np.argmax(np.ptp(vertices, axis=0)))
        position = vertices[faces[:, 0], axis]
        edges = np.quantile(position, np.linspace(0, 1, count + 1)[1:-1])
        piece = np.searchsorted(edges, position)
        groups = [faces[piece == k] for k in range(count)]
    pieces = []
    for group in groups:
        if not len(group):
            continue
        used, local_faces = np.unique(group, return_inverse=True)
        local_faces = local_faces.reshape(-1, 3).astype(np.int64)
        share = max(int(target * len(group) / len(faces)), 4)
        _, _, collapses = fast_simplification.simplify(
            vertices[used],
            local_faces,
            target_count=share,
            return_collapses=True,
            preserve_border=True,
        )
        points, triangles, local = fast_simplification.replay_simplification(
            vertices[used], local_faces, collapses
        )
        del collapses
        pieces.append((used, np.asarray(points), np.asarray(triangles), np.asarray(local)))
    return pieces


def _merge_for_simplifying(
    vertices: np.ndarray,
    faces: np.ndarray,
    uv: np.ndarray | None,
    colours: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Join vertices that are the same in position, UV and colour: files that store
    every triangle separately ("triangle soup", e.g. from STL) would otherwise have only
    borders, and nothing could be simplified. Returns the joined positions, the faces
    on them (degenerate ones dropped) and, per original vertex, its joined index."""
    key = np.hstack([vertices] + [a.astype(np.float64) for a in (uv, colours) if a is not None])
    unique, inverse = trimesh.grouping.unique_rows(key, digits=8)  # type: ignore[no-untyped-call]
    del key
    merged_faces = np.asarray(inverse, dtype=np.int64)[np.asarray(faces, dtype=np.int64)]
    proper = (
        (merged_faces[:, 0] != merged_faces[:, 1])
        & (merged_faces[:, 1] != merged_faces[:, 2])
        & (merged_faces[:, 0] != merged_faces[:, 2])
    )
    return vertices[unique], merged_faces[proper], np.asarray(inverse, dtype=np.int64)


def simplify_mesh(mesh: trimesh.Trimesh, target: int) -> tuple[trimesh.Trimesh, np.ndarray]:
    """Simplify to about ``target`` triangles, keeping UVs, colours and normals.

    Returns the mesh and, per original vertex, the index of its simplified vertex
    (-1 for unused vertices).
    """
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    colours = _colours(mesh)
    source = mesh.visual
    uv_in = (
        np.asarray(source.uv)
        if isinstance(source, trimesh.visual.TextureVisuals) and source.uv is not None
        else None
    )
    merged, merged_faces, inverse = _merge_for_simplifying(vertices, mesh.faces, uv_in, colours)
    del vertices
    pieces = _collapse(merged, merged_faces, target)
    original_normals = _vertex_normals(mesh)
    parts_points, parts_triangles, parts_normals = [], [], []
    parts_uv, parts_colours = [], []
    mapping = np.full(len(inverse), -1, dtype=np.int64)
    offset = 0
    for used, points, triangles, local in pieces:
        # Original vertices → this piece's simplified vertices (-1: not in this piece).
        local_of_merged = np.full(len(merged), -1, dtype=np.int64)
        local_of_merged[used] = np.arange(len(used))
        in_piece = local_of_merged[inverse]
        piece_map = np.where(in_piece >= 0, local[np.maximum(in_piece, 0)], -1)
        count = len(points)
        normals = _average(original_normals, piece_map, count)
        lengths = np.linalg.norm(normals, axis=1)
        lengths[lengths == 0] = 1.0
        parts_normals.append(normals / lengths[:, None])
        if uv_in is not None:
            parts_uv.append(_average(uv_in, piece_map, count))
        if colours is not None:
            parts_colours.append(_average(colours, piece_map, count))
        first = (mapping < 0) & (piece_map >= 0)  # a cut's vertices: the first piece's copy
        mapping[first] = piece_map[first] + offset
        parts_points.append(points)
        parts_triangles.append(triangles + offset)
        offset += count
    points = np.concatenate(parts_points)
    triangles = np.concatenate(parts_triangles)
    normals = np.concatenate(parts_normals)
    count = len(points)

    averaged_colours = (
        None
        if colours is None
        else np.clip(np.round(np.concatenate(parts_colours)), 0, 255).astype(np.uint8)
    )
    visual: trimesh.visual.ColorVisuals | trimesh.visual.TextureVisuals | None = None
    if isinstance(source, trimesh.visual.TextureVisuals) and uv_in is not None:
        uv = np.concatenate(parts_uv)
        visual = trimesh.visual.TextureVisuals(  # type: ignore[no-untyped-call]
            uv=uv, material=_small_material(source.material)
        )
        if averaged_colours is not None:
            visual.vertex_attributes["color"] = averaged_colours
    elif averaged_colours is not None:
        visual = trimesh.visual.ColorVisuals(vertex_colors=averaged_colours)
    elif isinstance(source, trimesh.visual.TextureVisuals):
        visual = trimesh.visual.TextureVisuals(  # type: ignore[no-untyped-call]
            material=_small_material(source.material)
        )

    simplified = trimesh.Trimesh(
        vertices=points,
        faces=triangles,
        vertex_normals=normals,
        visual=visual,
        process=False,
    )
    return simplified, mapping


def _level_scene(source: trimesh.Scene, geometry: dict[str, trimesh.Trimesh]) -> trimesh.Scene:
    """The same nodes and transforms with some geometry replaced (others shared)."""
    level = trimesh.Scene()
    level.geometry.update({**source.geometry, **geometry})
    level.graph = source.graph.copy()  # type: ignore[no-untyped-call]
    return level


def build_levels(scene: trimesh.Scene) -> list[LodLevel]:
    """Simplified copies of ``scene``, each about a quarter of the previous one."""
    original = {n: g for n, g in scene.geometry.items() if isinstance(g, trimesh.Trimesh)}
    total = sum(len(g.faces) for g in original.values())
    diagonal = float(np.linalg.norm(scene.extents)) or 1.0
    # Per part: each original vertex's index in the current level.
    to_level = {n: np.arange(len(g.vertices)) for n, g in original.items()}
    levels: list[LodLevel] = []
    source = scene
    for number, target in enumerate(level_targets(total), start=1):
        started = time.perf_counter()
        current = {n: g for n, g in source.geometry.items() if isinstance(g, trimesh.Trimesh)}
        current_total = sum(len(g.faces) for g in current.values())
        replaced: dict[str, trimesh.Trimesh] = {}
        for name, mesh in current.items():
            share = int(target * len(mesh.faces) / current_total)
            if len(mesh.faces) < LOD_MIN_PART_TRIANGLES or share >= len(mesh.faces):
                continue
            simplified, mapping = simplify_mesh(mesh, max(share, LOD_MIN_PART_TRIANGLES // 4))
            replaced[name] = simplified
            previous = to_level[name]
            to_level[name] = np.where(previous >= 0, mapping[np.maximum(previous, 0)], -1)
        level = _level_scene(source, replaced)
        shifts = []
        for name, mesh in original.items():
            index = to_level[name]
            used = index >= 0
            points = np.asarray(level.geometry[name].vertices)
            shifts.append(np.linalg.norm(mesh.vertices[used] - points[index[used]], axis=1))
        moved = np.concatenate(shifts) / diagonal if shifts else np.zeros(1)
        bounds_change = float(np.abs(level.bounds - scene.bounds).max()) / diagonal
        level_triangles = sum(
            len(g.faces) for g in level.geometry.values() if isinstance(g, trimesh.Trimesh)
        )
        if level_triangles > LOD_MIN_REDUCTION * current_total:
            # Nothing much could be simplified (e.g. triangles that share no edges are all
            # border, which is kept): such a level would only cost memory.
            log.warning(
                "LOD level %d dropped: %d of %d triangles left",
                number,
                level_triangles,
                current_total,
            )
            break
        if bounds_change > LOD_MAX_BOUNDS_CHANGE or float(moved.mean()) > LOD_MAX_MEAN_SHIFT:
            log.warning(
                "LOD level %d dropped: bounds changed %.2f%%, mean shift %.2f%%",
                number,
                bounds_change * 100,
                float(moved.mean()) * 100,
            )
            break
        buffer = io.BytesIO()
        level.export(buffer, file_type="glb")  # type: ignore[no-untyped-call]
        triangles = sum(
            len(g.faces) for g in level.geometry.values() if isinstance(g, trimesh.Trimesh)
        )
        levels.append(
            LodLevel(
                level=number,
                triangles=triangles,
                mean_shift=float(moved.mean()),
                max_shift=float(moved.max()),
                seconds=time.perf_counter() - started,
                glb=buffer.getvalue(),
            )
        )
        source = level  # the next level simplifies this one (faster, same result)
    return levels


# Settings a separate process needs to build the same levels (see build_levels_from_file).
_LIMITS = (
    "LOD_MIN_TRIANGLES",
    "LOD_STEP",
    "LOD_MIN_LEVEL_TRIANGLES",
    "LOD_MIN_PART_TRIANGLES",
    "LOD_TEXTURE_MAX",
    "LOD_MAX_BOUNDS_CHANGE",
    "LOD_MAX_MEAN_SHIFT",
    "LOD_MIN_REDUCTION",
    "LOD_PIECE_TRIANGLES",
)


def current_limits() -> dict[str, float]:
    return {name: globals()[name] for name in _LIMITS}


def build_levels_from_file(path: str, limits: dict[str, float]) -> list[LodLevel]:
    """``build_levels`` for a GLB file, meant to run in its own process: the simplifier
    holds Python's interpreter lock for seconds at a time (a server thread running it
    stops answering requests), and the process's memory is returned when it ends."""
    from ai3d.processing.mesh import load_model_scene  # noqa: PLC0415

    globals().update({name: limits[name] for name in _LIMITS if name in limits})
    return build_levels(load_model_scene(Path(path)))
