"""Quad topology kept beside the triangle mesh (D60); Blender runs in test_blender.py."""

import numpy as np
import trimesh
from trimesh.exchange.obj import export_obj

from ai3d.processing import quads


def _quad_grid(n: int = 11) -> tuple[np.ndarray, np.ndarray]:
    u, v = np.meshgrid(np.arange(n), np.arange(n))
    vertices = np.stack([u.ravel(), np.sin(u.ravel() / 3), v.ravel()], 1).astype(float) * 0.1
    index = np.arange(n * n).reshape(n, n)
    corners = np.stack(
        [
            index[:-1, :-1].ravel(),
            index[1:, :-1].ravel(),
            index[1:, 1:].ravel(),
            index[:-1, 1:].ravel(),
        ],
        axis=1,
    )
    return vertices, corners


def _triangulated(vertices: np.ndarray, corners: np.ndarray) -> trimesh.Scene:
    """Each quad as two triangles, diagonals mixed, face order shuffled, node moved."""
    triangles = []
    for k, (a, b, c, d) in enumerate(corners):
        triangles += [[a, b, c], [a, c, d]] if k % 2 else [[a, b, d], [b, c, d]]
    order = np.random.default_rng(0).permutation(len(triangles))
    mesh = trimesh.Trimesh(vertices, np.array(triangles)[order], process=False)
    soup = trimesh.creation.box(extents=(0.1, 0.1, 0.1))  # a part without quads
    scene = trimesh.Scene()
    move = trimesh.transformations.translation_matrix([1, 2, 3])
    scene.add_geometry(mesh, geom_name="Grid", node_name="GridNode", transform=move)
    scene.add_geometry(soup, geom_name="Box", node_name="Box")
    return scene


def test_quads_are_found_and_written_to_obj() -> None:
    vertices, corners = _quad_grid()
    scene = _triangulated(vertices, corners)
    pairs = quads.pair_triangles(scene, vertices[corners] + [1, 2, 3])
    assert set(pairs) == {"Grid"}
    assert len(pairs["Grid"]) == 100
    assert len(quads.check_pairs(scene.geometry["Grid"], pairs["Grid"])) == 100

    text, joined = quads.obj_with_quads(export_obj(scene), scene, pairs)
    assert joined == 100
    assert quads.count_obj_faces(text) == (100, 12)  # 100 quads; the box's 12 triangles
    # Re-read: the same surface (trimesh splits quads into two triangles again).
    reread = trimesh.load(
        trimesh.util.wrap_as_stream(text.encode()), file_type="obj", force="scene"
    )
    total = sum(g.area for g in reread.geometry.values())
    assert abs(total - sum(g.area for g in scene.geometry.values())) < 1e-6


def test_quads_keep_their_winding() -> None:
    vertices, corners = _quad_grid(3)
    scene = _triangulated(vertices, corners)
    pairs = quads.pair_triangles(scene, vertices[corners] + [1, 2, 3])
    text, _ = quads.obj_with_quads(export_obj(scene), scene, pairs)
    mesh = scene.geometry["Grid"]
    normal = mesh.face_normals.mean(axis=0)
    obj_vertices = np.array(
        [list(map(float, line.split()[1:4])) for line in text.splitlines() if line.startswith("v ")]
    )
    for line in text.splitlines():
        if line.startswith("f ") and len(line.split()) == 5:
            a, b, c, _ = (int(t.split("/")[0]) - 1 for t in line.split()[1:])
            facing = np.cross(obj_vertices[b] - obj_vertices[a], obj_vertices[c] - obj_vertices[a])
            assert np.dot(facing, normal) > 0  # same side as the triangles


def test_pairs_that_no_longer_match_are_dropped() -> None:
    vertices, corners = _quad_grid()
    scene = _triangulated(vertices, corners)
    pairs = quads.pair_triangles(scene, vertices[corners] + [1, 2, 3])["Grid"]
    edited = scene.geometry["Grid"].copy()
    edited.update_faces(np.arange(len(edited.faces)) % 7 != 0)  # parts deleted
    assert len(quads.check_pairs(edited, pairs)) < len(pairs)
    assert len(quads.check_pairs(edited, np.array([[0, 10**6]]))) == 0
