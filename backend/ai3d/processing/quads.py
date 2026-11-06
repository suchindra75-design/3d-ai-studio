"""Quad topology from retopology, kept beside the triangle mesh (D60).

GLB (glTF) stores triangles only, so a QuadriFlow result is saved as two triangles
per quad. Blender reports the corners of every quad it made; here each quad is
matched to the two triangles of the saved mesh that cover it, and the pairs are
stored with the version. OBJ and FBX exports join each pair back into one quad.

Only quads Blender actually made are paired; nothing is guessed from triangle
shapes. A version edited in the browser has no pairs (its exports are triangles).
"""

from __future__ import annotations

import numpy as np
import trimesh

# Positions closer than this (× the model's diagonal) are the same point.
_TOLERANCE = 1e-5


def _labels(points: np.ndarray, tolerance: float) -> np.ndarray:
    """One label per point; points within ``tolerance`` of each other share it."""
    from scipy.sparse import coo_matrix  # noqa: PLC0415
    from scipy.sparse.csgraph import connected_components  # noqa: PLC0415
    from scipy.spatial import cKDTree  # noqa: PLC0415

    pairs = cKDTree(points).query_pairs(tolerance, output_type="ndarray")
    count = len(points)
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(count, count))
    _, labels = connected_components(graph, directed=False)
    return np.asarray(labels, dtype=np.int64)


def _keys(labels: np.ndarray) -> np.ndarray:
    """One integer per unordered triple of labels (labels < 2**21)."""
    ordered = np.sort(labels, axis=-1)
    keys: np.ndarray = (ordered[..., 0] << 42) | (ordered[..., 1] << 21) | ordered[..., 2]
    return keys


def pair_triangles(scene: trimesh.Scene, corners: np.ndarray) -> dict[str, np.ndarray]:
    """Match quads (``corners``: (Q, 4, 3) world positions, glTF axes, in order around
    each quad) to triangle pairs. Returns, per geometry name, (P, 2) face indices."""
    from scipy.spatial import cKDTree  # noqa: PLC0415

    diagonal = float(np.linalg.norm(scene.extents)) or 1.0
    tolerance = diagonal * _TOLERANCE
    flat = corners.reshape(-1, 3)
    found: dict[str, np.ndarray] = {}
    for node in scene.graph.nodes_geometry:
        transform, name = scene.graph[node]
        mesh = scene.geometry.get(name)
        if not isinstance(mesh, trimesh.Trimesh) or str(name) in found:
            continue
        world = trimesh.transform_points(mesh.vertices, transform)
        labels = _labels(world, tolerance)
        if labels.max(initial=0) >= 2**21:
            continue  # too many points to key this way; left as triangles
        distance, nearest = cKDTree(world).query(flat)
        corner_labels = np.where(distance <= tolerance, labels[nearest], -1).reshape(-1, 4)
        usable = np.all(corner_labels >= 0, axis=1)
        face_keys = _keys(labels[mesh.faces])
        order = np.argsort(face_keys)
        sorted_keys = face_keys[order]

        def lookup(
            triangles: np.ndarray, sorted_keys: np.ndarray = sorted_keys, order: np.ndarray = order
        ) -> np.ndarray:
            keys = _keys(triangles)
            at = np.clip(np.searchsorted(sorted_keys, keys), 0, len(sorted_keys) - 1)
            return np.where(sorted_keys[at] == keys, order[at], -1)

        c = corner_labels
        pairs = []
        for first, second in (((0, 1, 2), (0, 2, 3)), ((0, 1, 3), (1, 2, 3))):
            a, b = lookup(c[:, first]), lookup(c[:, second])
            pairs.append(np.stack([a, b], axis=1))
        diagonal_a, diagonal_b = pairs
        ok_a = usable & np.all(diagonal_a >= 0, axis=1)
        ok_b = usable & ~ok_a & np.all(diagonal_b >= 0, axis=1)
        chosen = np.concatenate([diagonal_a[ok_a], diagonal_b[ok_b]])
        # A triangle belongs to one quad at most.
        _, first_use = np.unique(chosen.ravel(), return_index=True)
        unique_ok = np.zeros(chosen.size, dtype=bool)
        unique_ok[first_use] = True
        chosen = chosen[unique_ok.reshape(-1, 2).all(axis=1)]
        if len(chosen):
            found[str(name)] = chosen.astype(np.int64)
    return found


def check_pairs(mesh: trimesh.Trimesh, pairs: np.ndarray) -> np.ndarray:
    """The pairs that still describe a quad of ``mesh``: both faces exist and share an
    edge (two corners). Anything else (an edited mesh) is dropped."""
    if pairs.size == 0 or pairs.max() >= len(mesh.faces):
        return np.zeros((0, 2), dtype=np.int64)
    a, b = mesh.faces[pairs[:, 0]], mesh.faces[pairs[:, 1]]
    shared = (a[:, :, None] == b[:, None, :]).any(axis=2).sum(axis=1)
    kept: np.ndarray = pairs[shared == 2]
    return kept


def _join(first: list[str], second: list[str]) -> list[str] | None:
    """Two triangle face tokens ("v/vt/vn") sharing an edge → the quad's four tokens,
    in the first triangle's winding."""
    index = [t.split("/")[0] for t in first]
    other = [t.split("/")[0] for t in second]
    shared = [i for i in index if i in other]
    if len(shared) != 2:
        return None
    lone = next(i for i in range(3) if index[i] not in other)
    u, p, q = first[lone], first[(lone + 1) % 3], first[(lone + 2) % 3]
    w = next(t for t in second if t.split("/")[0] not in index)
    return [u, p, w, q]


def obj_with_quads(
    text: str, scene: trimesh.Scene, pairs: dict[str, np.ndarray]
) -> tuple[str, int]:
    """Rewrite trimesh's OBJ text (an ``o`` block per object, named after its node or
    its geometry, faces in geometry order) so each pair is one ``f`` line with four
    corners. Returns the text and the quad count."""
    node_geometry = {str(node): str(scene.graph[node][1]) for node in scene.graph.nodes_geometry}
    blocks = sum(1 for line in text.splitlines() if line.startswith("o "))
    only = next(iter(pairs)) if len(pairs) == 1 and blocks <= 1 else None
    out: list[str] = []
    block: list[str] = []
    name: str | None = None
    quads = 0

    def flush() -> None:
        nonlocal quads
        face_lines = [i for i, line in enumerate(block) if line.startswith("f ")]
        # trimesh names an object after its node or its geometry.
        geometry = node_geometry.get(name or "", name)
        if geometry not in pairs:
            geometry = only
        partner: dict[int, int] = {}
        if geometry in pairs:
            for a, b in pairs[geometry]:
                if a < len(face_lines) and b < len(face_lines):
                    partner[int(a)] = int(b)
        skip: set[int] = set()
        replace: dict[int, str] = {}
        for a, b in partner.items():
            joined = _join(block[face_lines[a]].split()[1:], block[face_lines[b]].split()[1:])
            if joined is None:
                continue
            replace[face_lines[a]] = "f " + " ".join(joined)
            skip.add(face_lines[b])
            quads += 1
        out.extend(replace.get(i, line) for i, line in enumerate(block) if i not in skip)
        block.clear()

    for line in text.splitlines():
        if line.startswith("o "):
            flush()
            name = line[2:].strip()
        block.append(line)
    flush()
    return "\n".join(out) + "\n", quads


def count_obj_faces(text: str) -> tuple[int, int]:
    """(quads, triangles) among the ``f`` lines of an OBJ file."""
    quads = triangles = 0
    for line in text.splitlines():
        if line.startswith("f "):
            corners = len(line.split()) - 1
            if corners == 4:
                quads += 1
            elif corners == 3:
                triangles += 1
    return quads, triangles


def pair_centres(scene: trimesh.Scene, pairs: dict[str, np.ndarray]) -> np.ndarray:
    """World-space centres (glTF axes) of each pair's two triangles: (P, 2, 3). Blender
    finds the faces by these after importing the GLB and joins them."""
    centres = []
    for node in scene.graph.nodes_geometry:
        transform, name = scene.graph[node]
        mesh = scene.geometry.get(name)
        if name not in pairs or not isinstance(mesh, trimesh.Trimesh):
            continue
        world = trimesh.transform_points(mesh.vertices, transform)
        triangle_centres = world[mesh.faces].mean(axis=1)
        centres.append(triangle_centres[pairs[name]])
    return np.concatenate(centres) if centres else np.zeros((0, 2, 3))
