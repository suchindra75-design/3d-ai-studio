"""Individual, explicit repairs (D70). Each runs only when asked for, reports exactly
what it changed, and works on a copy that becomes a new version: the original
version is never touched (services/repair.py runs them as a job).

What each keeps (UVs, materials, colours and textures stay on every triangle that
stays, unless said otherwise):

- recalculate_normals: changes normals only.
- fix_winding: turns triangles round; nothing moves.
- remove_degenerate_faces / remove_duplicate_faces: removes triangles that draw
  nothing or repeat another; the visible surface does not move.
- merge_duplicate_vertices: merges vertices identical in every attribute.
- fill_small_holes: adds flat triangles in openings of 3 or 4 edges (not on
  textured or coloured meshes: the new triangles would have no texture or colour).
- separate_components: splits pieces into separate parts; no geometry changes.
- remove_components: deletes the pieces named (destructive: only those chosen).
- repair_non_manifold: removes duplicate and zero-area triangles where they make
  edges non-manifold; other non-manifold edges are reported, not changed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import trimesh

from ai3d.processing import geometry
from ai3d.processing import inspect as ins
from ai3d.processing.geometry import ProcessingResult, Step


@dataclass(frozen=True, slots=True)
class Operation:
    code: str
    title: str
    changes_shape: bool  # can the visible surface change?
    destructive: bool  # does it delete geometry the user can see?
    note: str


OPERATIONS: dict[str, Operation] = {
    op.code: op
    for op in (
        Operation(
            "recalculate_normals",
            "Recalculate normals",
            False,
            False,
            "Normals only: computed from the triangles, smooth across texture seams "
            "(hard edges set by hand become smooth).",
        ),
        Operation(
            "fix_winding",
            "Fix winding",
            False,
            False,
            "Turns triangles round so neighbours agree and closed pieces face out.",
        ),
        Operation(
            "remove_degenerate_faces",
            "Remove zero-area triangles",
            False,
            False,
            "Removes triangles with no area or invalid corners.",
        ),
        Operation(
            "remove_duplicate_faces",
            "Remove duplicate triangles",
            False,
            False,
            "Removes triangles that repeat another's corners.",
        ),
        Operation(
            "merge_duplicate_vertices",
            "Merge duplicate vertices",
            False,
            False,
            "Merges vertices identical in position, UVs, normals and colours.",
        ),
        Operation(
            "fill_small_holes",
            "Fill small holes",
            True,
            False,
            "Adds flat triangles in openings of three or four edges; untextured, "
            "uncoloured meshes only.",
        ),
        Operation(
            "separate_components",
            "Separate pieces",
            False,
            False,
            "Each piece becomes its own part; no geometry changes.",
        ),
        Operation(
            "remove_components",
            "Remove chosen pieces",
            True,
            True,
            "Deletes the pieces you chose, and only those.",
        ),
        Operation(
            "repair_non_manifold",
            "Repair non-manifold edges",
            False,
            False,
            "Removes duplicate and zero-area triangles where they cause non-manifold "
            "edges; others are reported, not changed.",
        ),
    )
}


def _meshes(scene: trimesh.Scene) -> list[tuple[str, trimesh.Trimesh]]:
    return [(n, g) for n, g in scene.geometry.items() if isinstance(g, trimesh.Trimesh)]


_CARRIED_NORMALS = "_ai3d_stored_normals"


def _carry_normals(scene: trimesh.Scene) -> None:
    """trimesh forgets a mesh's stored normals whenever its faces change (they live in a
    cache that is cleared), so removing a triangle would also drop the file's normals
    and with them hand-set hard edges. Carry them as a vertex attribute, which trimesh
    re-indexes along with the vertices when they are removed, merged or split."""
    for _, mesh in _meshes(scene):
        stored = mesh._cache.cache.get("vertex_normals")
        if stored is not None and len(stored) == len(mesh.vertices):
            mesh.vertex_attributes[_CARRIED_NORMALS] = np.array(stored, dtype=np.float64)


def _carried(mesh: trimesh.Trimesh) -> np.ndarray | None:
    carried = mesh.vertex_attributes.get(_CARRIED_NORMALS)
    return None if carried is None else np.asarray(carried)


def _restore_normals(scene: trimesh.Scene) -> None:
    for _, mesh in _meshes(scene):
        carried = _carried(mesh)
        mesh.vertex_attributes.pop(_CARRIED_NORMALS, None)
        if carried is not None and len(carried) == len(mesh.vertices):
            mesh._cache["vertex_normals"] = np.asarray(carried)  # as trimesh's setter stores them


def _welded_faces(mesh: trimesh.Trimesh) -> np.ndarray:
    """The mesh's faces over welded positions (seams joined), as the inspection sees them."""
    extent = float(np.ptp(mesh.vertices, axis=0).max()) if len(mesh.vertices) else 1.0
    quantised = np.round(np.nan_to_num(mesh.vertices) / ((extent or 1.0) * 1e-7)).astype(np.int64)
    _, ids = trimesh.grouping.unique_rows(quantised)  # type: ignore[no-untyped-call]
    faces: np.ndarray = np.asarray(ids)[mesh.faces]
    return faces


def recalculate_normals(scene: trimesh.Scene) -> Step:
    title = OPERATIONS["recalculate_normals"].title
    changed = total = 0
    for _, mesh in _meshes(scene):
        welded = np.asarray(_welded_faces(mesh))
        corners = mesh.vertices[mesh.faces]
        face_normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
        accumulated = np.zeros((int(welded.max()) + 1, 3))
        for k in range(3):  # area-weighted, over welded positions
            np.add.at(accumulated, welded[:, k], face_normals)
        lengths = np.linalg.norm(accumulated, axis=1, keepdims=True)
        unit = accumulated / np.maximum(lengths, 1e-300)
        ids = np.zeros(len(mesh.vertices), dtype=np.int64)
        ids[mesh.faces.ravel()] = welded.ravel()
        new = unit[ids]
        stored = mesh._cache.cache.get("vertex_normals")  # what the file held
        if stored is not None and len(stored) == len(new):
            dot = np.einsum("ij,ij->i", np.asarray(stored, dtype=np.float64), new)
            changed += int((~np.isfinite(dot) | (dot < np.cos(np.radians(1)))).sum())
        else:
            changed += len(new)
        total += len(new)
        mesh._cache["vertex_normals"] = new  # as trimesh's own setter stores them
    if not total:
        return Step("recalculate_normals", title, "skipped", "No triangle meshes.")
    return Step(
        "recalculate_normals",
        title,
        "applied",
        f"Recomputed the normals of {total:,} vertices from the triangles; {changed:,} differ by "
        "more than 1° from what was stored. Positions, UVs and materials are unchanged; hard "
        "edges set by hand are now smooth where vertices are shared.",
    )


def _remove(scene: trimesh.Scene, select: Callable[[trimesh.Trimesh], np.ndarray]) -> int:
    """Remove the faces ``select`` marks in each mesh; returns how many."""
    removed = 0
    for _, mesh in _meshes(scene):
        drop = select(mesh)
        if drop.any():
            removed += geometry._keep_faces(mesh, ~drop)
    return removed


def _degenerate(mesh: trimesh.Trimesh) -> np.ndarray:
    w = _welded_faces(mesh)
    corners = np.nan_to_num(mesh.vertices)[mesh.faces]
    area = (
        np.linalg.norm(
            np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1
        )
        / 2
    )
    extent = float(np.ptp(mesh.vertices, axis=0).max()) or 1.0
    invalid = ~np.isfinite(mesh.vertices[mesh.faces]).all(axis=(1, 2))
    repeated = (w[:, 0] == w[:, 1]) | (w[:, 1] == w[:, 2]) | (w[:, 0] == w[:, 2])
    result: np.ndarray = invalid | repeated | (area <= (extent * 1e-8) ** 2)
    return result


def _duplicates(mesh: trimesh.Trimesh) -> np.ndarray:
    from ai3d.processing.mesh import duplicate_face_count  # noqa: PLC0415

    w = _welded_faces(mesh)
    _, first = duplicate_face_count(w, int(w.max()) + 1)
    drop = np.ones(len(mesh.faces), dtype=bool)
    drop[first] = False
    return drop


def remove_degenerate_faces(scene: trimesh.Scene) -> Step:
    removed = _remove(scene, _degenerate)
    title = OPERATIONS["remove_degenerate_faces"].title
    if not removed:
        return Step("remove_degenerate_faces", title, "skipped", "No zero-area triangles.")
    return Step(
        "remove_degenerate_faces",
        title,
        "applied",
        f"Removed {removed:,} triangles with no area or invalid corners. The visible surface "
        "did not move; UVs and materials of the other triangles are unchanged.",
    )


def remove_duplicate_faces(scene: trimesh.Scene) -> Step:
    removed = _remove(scene, _duplicates)
    title = OPERATIONS["remove_duplicate_faces"].title
    if not removed:
        return Step("remove_duplicate_faces", title, "skipped", "No duplicate triangles.")
    return Step(
        "remove_duplicate_faces",
        title,
        "applied",
        f"Removed {removed:,} triangles that repeated another's corners (the first of each "
        "is kept, with its UVs and material). The surface did not move.",
    )


def merge_duplicate_vertices(scene: trimesh.Scene) -> Step:
    merged = 0
    for _, mesh in _meshes(scene):
        before = len(mesh.vertices)
        # Position, UVs and normals must all match (seams and hard edges stay).
        mesh.merge_vertices(merge_tex=False, merge_norm=False)
        merged += before - len(mesh.vertices)
    title = OPERATIONS["merge_duplicate_vertices"].title
    if not merged:
        return Step("merge_duplicate_vertices", title, "skipped", "No exact duplicates.")
    return Step(
        "merge_duplicate_vertices",
        title,
        "applied",
        f"Merged {merged:,} vertices identical in position, UVs and normals to another. "
        "Nothing moved; texture seams and hard edges are kept.",
    )


def _position_keys(vertices: np.ndarray) -> np.ndarray:
    rows = np.ascontiguousarray(vertices, dtype=np.float64)
    keys: np.ndarray = rows.view(np.dtype((np.void, rows.itemsize * 3))).ravel()
    return keys


def _normals_at(old: np.ndarray, normals: np.ndarray, new: np.ndarray) -> np.ndarray | None:
    """Stored normals for ``new`` vertices, taken from ``old`` ones at the same position;
    None when vertices at one position had different normals (a hand-set hard edge) or a
    new vertex has no old one."""
    keys, first, inverse = np.unique(_position_keys(old), return_index=True, return_inverse=True)
    per_position = normals[first]
    if not np.allclose(per_position[inverse.ravel()], normals, atol=1e-6):
        return None
    wanted = _position_keys(new)
    where = np.minimum(np.searchsorted(keys, wanted), len(keys) - 1)
    if len(keys) == 0 or (keys[where] != wanted).any():
        return None
    found: np.ndarray = per_position[where]
    return found


def fill_small_holes(scene: trimesh.Scene) -> Step:
    before = dict(_meshes(scene))
    step = geometry.fill_holes(scene)
    lost = 0
    for name, mesh in _meshes(scene):
        # Filling works on a copy with vertices at one position joined, which becomes
        # the mesh: hand the stored normals on by position where that is unambiguous.
        old = before.get(name)
        carried = None if old is None else _carried(old)
        if old is None or mesh is old or carried is None:
            continue
        normals = _normals_at(old.vertices, carried, mesh.vertices)
        if normals is None:
            lost += 1
        else:
            mesh.vertex_attributes[_CARRIED_NORMALS] = normals
    detail = step.detail
    if step.status == "applied":
        detail += (
            " Only openings of three or four edges are filled; larger ones are left as they are."
        )
    if lost:
        detail += (
            f" The stored normals of {lost:,} mesh(es) could not be kept (vertices at one point "
            "had different normals, and filling joins them); the viewer computes smooth ones."
        )
    return Step("fill_small_holes", OPERATIONS["fill_small_holes"].title, step.status, detail)


def separate_components(scene: trimesh.Scene) -> Step:
    parts = 0
    for name, mesh in _meshes(scene):
        # As mesh.split does, keeping each piece's faces to carry stored normals along.
        components = trimesh.graph.connected_components(
            edges=mesh.face_adjacency, nodes=np.arange(len(mesh.faces)), min_len=1
        )
        if len(components) <= 1:
            continue
        pieces: list[trimesh.Trimesh] = list(
            mesh.submesh(components, only_watertight=False, append=False)  # type: ignore[arg-type]
        )
        carried = _carried(mesh)
        if carried is not None:
            for piece, faces in zip(pieces, components, strict=True):
                piece.vertex_attributes[_CARRIED_NORMALS] = carried[np.unique(mesh.faces[faces])]
        nodes = [n for n in scene.graph.nodes_geometry if scene.graph[n][1] == name]
        transforms = [scene.graph[n][0] for n in nodes]
        scene.delete_geometry(name)
        for i, piece in enumerate(pieces):
            for t, transform in enumerate(transforms):
                node = f"{name}_piece{i + 1}" + (f"_{t + 1}" if len(transforms) > 1 else "")
                scene.add_geometry(
                    piece, geom_name=f"{name}_piece{i + 1}", node_name=node, transform=transform
                )
        parts += len(pieces)
    title = OPERATIONS["separate_components"].title
    if not parts:
        return Step("separate_components", title, "skipped", "Every mesh is one piece already.")
    return Step(
        "separate_components",
        title,
        "applied",
        f"Split into {parts:,} parts (pieces joined by shared edges). No geometry changed; each "
        "part keeps its UVs and material.",
    )


def remove_components(scene: trimesh.Scene, pieces: list[int]) -> Step:
    """Delete the pieces with these ids (as numbered by the inspection report)."""
    title = OPERATIONS["remove_components"].title
    if not pieces:
        return Step("remove_components", title, "skipped", "No pieces were chosen.")
    model = ins._gather(scene)
    edges = ins._edge_analysis(model)
    labels = ins._connected(edges.unique_pairs, model.welded_count)[model.welded_faces[:, 0]]
    chosen = np.isin(labels, np.asarray(pieces, dtype=np.int64))
    if chosen.all():
        return Step(
            "remove_components", title, "skipped", "Not done: that would remove every piece."
        )
    unknown = sorted(set(pieces) - set(np.unique(labels).tolist()))
    removed = 0
    names = list(scene.geometry)
    offset = 0
    for node in scene.graph.nodes_geometry:
        mesh_name = scene.graph[node][1]
        mesh = scene.geometry.get(mesh_name)
        if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces):
            continue
        drop = chosen[offset : offset + len(mesh.faces)]
        offset += len(mesh.faces)
        if drop.any() and mesh_name in names:
            removed += geometry._keep_faces(mesh, ~drop)
            names.remove(mesh_name)  # a mesh used by several nodes is changed once
    found = len(set(pieces) - set(unknown))
    detail = f"Removed {found:,} chosen piece(s): {removed:,} triangles deleted."
    if unknown:
        detail += f" Not found (the model changed since it was inspected?): {unknown[:20]}."
    return Step("remove_components", title, "applied" if removed else "skipped", detail)


def repair_non_manifold(scene: trimesh.Scene) -> Step:
    title = OPERATIONS["repair_non_manifold"].title
    before = ins.inspect_scene(scene, ins.Options(self_intersections="never"))
    count_before = before.measurements["topology"]["non_manifold_edges"]
    if not count_before:
        return Step("repair_non_manifold", title, "skipped", "No non-manifold edges.")
    removed = _remove(scene, lambda m: _duplicates(m) | _degenerate(m))
    after = ins.inspect_scene(scene, ins.Options(self_intersections="never"))
    left = after.measurements["topology"]["non_manifold_edges"]
    detail = (
        f"Removed {removed:,} duplicate or zero-area triangles; non-manifold edges "
        f"{count_before:,} → {left:,}."
    )
    if left:
        detail += (
            f" The remaining {left:,} are where three or more real surfaces meet; joining or "
            "separating them would change the shape, so they were left for manual editing."
        )
    return Step("repair_non_manifold", title, "applied" if removed else "skipped", detail)


def run_repairs(scene: trimesh.Scene, operations: list[dict[str, Any]]) -> ProcessingResult:
    """Run the operations in the order given; each records exactly what it changed.
    Stored normals stay as they were (on the vertices that stay) unless the operation
    is recalculate_normals."""
    result = ProcessingResult(scene=scene)
    for operation in operations:
        code = operation["code"]
        if code == "recalculate_normals":
            result.steps.append(recalculate_normals(scene))
            continue
        _carry_normals(scene)
        try:
            if code == "remove_components":
                step = remove_components(scene, [int(p) for p in operation.get("pieces") or []])
            elif code == "fix_winding":
                step = geometry.fix_normals(scene)
                step = Step(
                    "fix_winding", OPERATIONS["fix_winding"].title, step.status, step.detail
                )
            else:
                step = _SIMPLE[code](scene)
        finally:
            _restore_normals(scene)
        result.steps.append(step)
    return result


_SIMPLE: dict[str, Callable[[trimesh.Scene], Step]] = {
    "recalculate_normals": recalculate_normals,
    "remove_degenerate_faces": remove_degenerate_faces,
    "remove_duplicate_faces": remove_duplicate_faces,
    "merge_duplicate_vertices": merge_duplicate_vertices,
    "fill_small_holes": fill_small_holes,
    "separate_components": separate_components,
    "repair_non_manifold": repair_non_manifold,
}
