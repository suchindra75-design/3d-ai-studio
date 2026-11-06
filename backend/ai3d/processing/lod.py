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

import fast_simplification
import numpy as np
import trimesh
from PIL import Image

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
    # Join vertices that are the same in position, UV and colour first: files that
    # store every triangle separately ("triangle soup", e.g. from STL) would otherwise
    # have only borders, and nothing could be simplified.
    key = np.hstack([vertices] + [a.astype(np.float64) for a in (uv_in, colours) if a is not None])
    unique, inverse = trimesh.grouping.unique_rows(key, digits=8)  # type: ignore[no-untyped-call]
    merged_faces = np.asarray(inverse, dtype=np.int64)[np.asarray(mesh.faces, dtype=np.int64)]
    proper = (
        (merged_faces[:, 0] != merged_faces[:, 1])
        & (merged_faces[:, 1] != merged_faces[:, 2])
        & (merged_faces[:, 0] != merged_faces[:, 2])
    )
    merged_faces = merged_faces[proper]
    merged = vertices[unique]
    _, _, collapses = fast_simplification.simplify(
        merged, merged_faces, target_count=target, return_collapses=True, preserve_border=True
    )
    points, triangles, merged_mapping = fast_simplification.replay_simplification(
        merged, merged_faces, collapses
    )
    mapping = np.asarray(merged_mapping)[inverse]
    count = len(points)

    normals = _average(np.asarray(mesh.vertex_normals), mapping, count)
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0
    normals /= lengths[:, None]

    averaged_colours = (
        None
        if colours is None
        else np.clip(np.round(_average(colours, mapping, count)), 0, 255).astype(np.uint8)
    )
    visual: trimesh.visual.ColorVisuals | trimesh.visual.TextureVisuals | None = None
    if isinstance(source, trimesh.visual.TextureVisuals) and uv_in is not None:
        uv = _average(uv_in, mapping, count)
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
