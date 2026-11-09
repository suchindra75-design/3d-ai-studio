"""Model health inspection (processing/inspect.py) on shapes whose problems are known."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from PIL import Image

from ai3d.processing import inspect as ins


def _scene(*meshes: trimesh.Trimesh) -> trimesh.Scene:
    scene = trimesh.Scene()
    for i, mesh in enumerate(meshes):
        scene.add_geometry(mesh, geom_name=f"part{i}", node_name=f"part{i}")
    return scene


def _codes(report: ins.Report) -> dict[str, ins.Finding]:
    return {f.code: f for f in report.findings}


def _ball() -> trimesh.Trimesh:
    return trimesh.creation.icosphere(subdivisions=3)


def test_a_clean_closed_model_has_no_problems() -> None:
    report = ins.inspect_scene(_scene(_ball()))
    serious = [f.code for f in report.findings if f.severity in {"error", "warning"}]
    assert serious == []
    assert report.measurements["topology"]["watertight"] is True
    assert report.measurements["topology"]["pieces"] == 1
    assert report.measurements["integrity"]["self_intersection_search"]["crossing_pairs"] == 0
    assert report.triangles == 1280


def test_seams_are_not_holes_and_exact_copies_are_duplicates() -> None:
    ball = _ball()
    ball.unmerge_vertices()  # every triangle its own corners
    rng = np.random.default_rng(0)
    ball.visual = trimesh.visual.TextureVisuals(uv=rng.random((len(ball.vertices), 2)))
    report = ins.inspect_scene(_scene(ball))  # copies differ in UVs: seams
    assert report.measurements["topology"]["watertight"] is True
    assert report.measurements["topology"]["vertices_split_at_seams"] == 3840 - 642
    assert "open_boundaries" not in _codes(report)
    assert "duplicate_vertices" not in _codes(report)
    plain = _ball()
    plain.unmerge_vertices()  # copies identical in everything: redundant
    found = _codes(ins.inspect_scene(_scene(plain)))["duplicate_vertices"]
    assert found.count == 3840 - 642
    assert found.repair == "merge_duplicate_vertices"


def test_a_missing_face_is_one_four_edge_hole() -> None:
    box = trimesh.creation.box()
    box.update_faces(np.arange(len(box.faces)) >= 2)  # one side's two triangles gone
    found = _codes(ins.inspect_scene(_scene(box)))["open_boundaries"]
    assert found.count == 1  # one opening
    assert found.details["open_edges"] == 4
    assert found.repair == "fill_small_holes"
    assert found.locations.total == 4
    assert len(found.locations.segments) == 4


def test_a_fin_makes_a_non_manifold_edge() -> None:
    box = trimesh.creation.box()
    a, b = box.faces[0][:2]
    fin = len(box.vertices)
    vertices = np.vstack([box.vertices, [[0, 2, 0]]])
    faces = np.vstack([box.faces, [[a, b, fin]]])
    report = ins.inspect_scene(_scene(trimesh.Trimesh(vertices, faces, process=False)))
    found = _codes(report)["non_manifold_edges"]
    assert found.count == 1
    assert found.severity == "error"
    assert report.measurements["topology"]["watertight"] is False


def test_degenerate_and_duplicate_triangles_are_counted_exactly() -> None:
    box = trimesh.creation.box()
    extra = [[0, 0, 1], [1, 1, 2], box.faces[3], box.faces[5]]  # 2 degenerate, 2 repeats
    mesh = trimesh.Trimesh(box.vertices, np.vstack([box.faces, extra]), process=False)
    codes = _codes(ins.inspect_scene(_scene(mesh)))
    assert codes["degenerate_faces"].count == 2
    assert codes["duplicate_faces"].count == 2
    assert codes["degenerate_faces"].repair == "remove_degenerate_faces"


def test_flipped_triangles_and_inside_out_pieces() -> None:
    ball = _ball()
    faces = ball.faces.copy()
    faces[:10] = faces[:10, ::-1]
    codes = _codes(ins.inspect_scene(_scene(trimesh.Trimesh(ball.vertices, faces, process=False))))
    assert codes["inconsistent_winding"].count > 0
    assert codes["inconsistent_winding"].repair == "fix_winding"
    inverted = _ball()
    inverted.invert()
    codes = _codes(ins.inspect_scene(_scene(inverted)))
    assert codes["inside_out"].count == 1
    assert "inconsistent_winding" not in codes


def test_separate_pieces_are_listed_largest_first() -> None:
    small = trimesh.creation.box(extents=(0.1, 0.1, 0.1))
    small.apply_translation([3, 0, 0])
    report = ins.inspect_scene(_scene(_ball(), small))
    found = _codes(report)["components"]
    assert found.count == 2
    sizes = [p["triangles"] for p in found.details["pieces"]]
    assert sizes == [1280, 12]
    assert found.details["pieces"][1]["centre"] == pytest.approx([3, 0, 0], abs=1e-9)


def _brute_force_pairs(report_model: Any) -> set[tuple[int, int]]:
    """Every pair of triangles tested (no grid); pairs whose bounding boxes do not
    overlap, which cannot cross, are dropped first, row by row."""
    corners = report_model.vertices[report_model.faces]
    low, high = corners.min(axis=1), corners.max(axis=1)
    a_all, b_all = [], []
    for i in range(len(corners) - 1):
        rest = np.arange(i + 1, len(corners))
        overlap = np.all((low[i] <= high[rest]) & (low[rest] <= high[i]), axis=1)
        a_all.append(np.full(int(overlap.sum()), i))
        b_all.append(rest[overlap])
    pairs = ins._crossing_pairs(report_model, np.concatenate(a_all), np.concatenate(b_all))
    return {(int(x), int(y)) for x, y in pairs}


def test_self_intersections_match_a_check_of_every_pair() -> None:
    one = trimesh.creation.box()
    two = trimesh.creation.box()
    two.apply_translation([0.5, 0.3, 0.2])  # pushed into the first
    merged = trimesh.util.concatenate([one, two])
    model = ins._gather(_scene(merged))
    expected = _brute_force_pairs(model)
    assert expected  # they do cross
    crossing, points, facts = ins._self_intersections(ins._Context(model, ins.Options()))
    found = _codes(ins.inspect_scene(_scene(merged)))["self_intersections"]
    assert found.count == len(expected) == facts["crossing_pairs"]
    assert set(crossing.tolist()) == {f for pair in expected for f in pair}
    assert len(points) == len(expected)
    apart = trimesh.creation.box()
    apart.apply_translation([3, 0, 0])
    report = ins.inspect_scene(_scene(trimesh.util.concatenate([one, apart])))
    assert "self_intersections" not in _codes(report)


def test_a_noisy_ball_finds_every_crossing_a_full_search_finds() -> None:
    ball = trimesh.creation.icosphere(subdivisions=2)
    rng = np.random.default_rng(4)
    ball.vertices = ball.vertices + rng.normal(0, 0.08, ball.vertices.shape)
    model = ins._gather(_scene(ball))
    expected = _brute_force_pairs(model)
    crossing, _, facts = ins._self_intersections(ins._Context(model, ins.Options()))
    assert facts["crossing_pairs"] == len(expected) > 0
    assert set(crossing.tolist()) == {f for pair in expected for f in pair}


def test_large_models_skip_self_intersections_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ins, "SELF_INTERSECTION_MAX_FACES", 100)
    report = ins.inspect_scene(_scene(_ball()))
    assert [s.code for s in report.skipped] == ["self_intersections"]
    report = ins.inspect_scene(_scene(_ball()), ins.Options(self_intersections="always"))
    assert report.skipped == []


def test_locations_are_capped_but_counts_are_not(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ins, "MAX_LOCATIONS", 5)
    ball = _ball()
    faces = np.vstack([ball.faces, ball.faces[:40]])
    found = _codes(ins.inspect_scene(_scene(trimesh.Trimesh(ball.vertices, faces, process=False))))
    duplicates = found["duplicate_faces"]
    assert duplicates.count == 40
    assert duplicates.locations.total == 40
    assert duplicates.locations.shown == len(duplicates.locations.triangles) == 5


def _thin_plate(thickness_m: float) -> trimesh.Trimesh:
    return trimesh.creation.box(extents=(0.02, thickness_m, 0.02))


def test_printability_measures_wall_thickness_in_millimetres() -> None:
    options = ins.Options(printability=True, min_wall_mm=1.0, units_per_mm=0.001)
    thick = ins.inspect_scene(_scene(_thin_plate(0.005)), options)
    wall = thick.measurements["fabrication"]["wall_thickness"]
    assert wall["unit"] == "mm"
    assert wall["median"] == pytest.approx(5, rel=0.05)  # a 5 mm plate, measured across
    assert "thin_walls" not in _codes(thick)
    thin = ins.inspect_scene(_scene(_thin_plate(0.0005)), options)
    found = _codes(thin)["thin_walls"]
    assert found.method == "sampled"
    assert thin.measurements["fabrication"]["wall_thickness"]["min"] == pytest.approx(0.5, rel=0.05)
    assert found.count > 0


def test_a_wall_exactly_at_the_limit_is_not_thin() -> None:
    # Rays start just inside the surface; the reading is from the surface itself, so a
    # 1 mm wall measures 1 mm (not 0.9999999) and passes a 1 mm limit.
    options = ins.Options(printability=True, min_wall_mm=1.0, units_per_mm=0.001)
    exact = ins.inspect_scene(_scene(_thin_plate(0.001)), options)
    assert exact.measurements["fabrication"]["wall_thickness"]["min"] == pytest.approx(
        1.0, abs=1e-6
    )
    assert "thin_walls" not in _codes(exact)
    under = ins.inspect_scene(_scene(_thin_plate(0.00099)), options)
    assert _codes(under)["thin_walls"].count > 0


def test_printability_without_a_scale_says_so() -> None:
    report = ins.inspect_scene(_scene(_ball()), ins.Options(printability=True, min_wall_mm=5))
    found = _codes(report)["thin_walls"]  # a radius-1 ball is under 5 units thick
    assert "no real-world scale" in found.explanation
    assert report.measurements["fabrication"]["units"] == "model units"


def test_an_open_model_is_not_printable_and_is_not_measured() -> None:
    box = trimesh.creation.box()
    box.update_faces(np.arange(len(box.faces)) >= 2)
    report = ins.inspect_scene(_scene(box), ins.Options(printability=True))
    assert _codes(report)["not_watertight"].severity == "error"
    assert "wall_thickness" not in report.measurements["fabrication"]


def test_overhangs_and_a_shell_inside_another() -> None:
    cone = trimesh.creation.cone(radius=2, height=0.5)  # point up: flat base on the plate
    cone.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))
    upright = ins.inspect_scene(_scene(cone), ins.Options(printability=True))
    assert "overhangs" not in _codes(upright)
    flipped = cone.copy()
    flipped.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0]))
    upside_down = ins.inspect_scene(_scene(flipped), ins.Options(printability=True))
    assert _codes(upside_down)["overhangs"].count > 0
    outer = trimesh.creation.box(extents=(2, 2, 2))
    inner = trimesh.creation.box(extents=(0.5, 0.5, 0.5))
    nested = ins.inspect_scene(_scene(outer, inner), ins.Options(printability=True))
    found = _codes(nested)["internal_shells"]
    assert found.count == 1
    assert found.method == "approximate"


def _textured_ball(uv: np.ndarray | None = None) -> trimesh.Trimesh:
    ball = _ball()
    ball.unmerge_vertices()
    rng = np.random.default_rng(1)
    image = Image.fromarray(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8))
    coords = uv if uv is not None else _spherical_uv(ball.vertices)
    ball.visual = trimesh.visual.TextureVisuals(uv=coords, image=image)
    return ball


def _spherical_uv(vertices: np.ndarray) -> np.ndarray:
    u = 0.5 + np.arctan2(vertices[:, 2], vertices[:, 0]) / (2 * np.pi)
    v = 0.5 + np.arcsin(np.clip(vertices[:, 1], -1, 1)) / np.pi
    return np.stack([u, v], axis=1)


def _report_from_file(tmp_path: Path, scene: trimesh.Scene) -> dict[str, Any]:
    path = tmp_path / "model.glb"
    path.write_bytes(scene.export(file_type="glb"))
    return ins.inspect_file(str(path), {})


def test_uv_and_texture_checks(tmp_path: Path) -> None:
    good = _report_from_file(tmp_path, _scene(_textured_ball()))
    codes = {f["code"] for f in good["findings"]}
    assert "missing_uvs" not in codes
    assert good["measurements"]["uv_texture"]["images"][0]["width"] == 64
    collapsed = _textured_ball(np.zeros((1280 * 3, 2)))
    found = {f["code"]: f for f in _report_from_file(tmp_path, _scene(collapsed))["findings"]}
    assert found["degenerate_uvs"]["count"] == 1280
    # Two separate islands sharing one part of the texture.
    twice = _textured_ball()
    other = twice.copy()
    other.apply_translation([3, 0, 0])
    one_mesh = trimesh.util.concatenate([twice, other])  # one texture, two islands on it
    both = _report_from_file(tmp_path, _scene(one_mesh))
    overlap = {f["code"]: f for f in both["findings"]}["uv_overlaps"]
    assert overlap["method"] == "approximate"
    assert overlap["count"] > 50  # % of the used texture


def _rewrite_json(path: Path, change: Any) -> None:
    data = path.read_bytes()
    length = struct.unpack("<I", data[12:16])[0]
    document = json.loads(data[20 : 20 + length])
    change(document)
    text = json.dumps(document).encode()
    text += b" " * (-len(text) % 4)
    rest = data[20 + length :]
    header = struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(text) + len(rest))
    path.write_bytes(header + struct.pack("<II", len(text), 0x4E4F534A) + text + rest)


def test_invalid_and_unused_materials_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "model.glb"
    path.write_bytes(_scene(_textured_ball()).export(file_type="glb"))

    def break_it(document: dict[str, Any]) -> None:
        document["materials"].append({"name": "Spare"})
        document["materials"].append(dict(document["materials"][0], name="Copy"))
        document["meshes"][0]["primitives"][0]["material"] = 7

    _rewrite_json(path, break_it)
    report = ins.inspect_file(str(path), {})  # trimesh cannot open it: reported, not raised
    codes = {f["code"]: f for f in report["findings"]}
    assert codes["unreadable"]["severity"] == "error"
    assert codes["invalid_references"]["count"] == 1
    assert "material 7" in codes["invalid_references"]["details"]["references"][0]
    assert codes["unused_materials"]["count"] == 3  # nothing uses any of them now
    assert codes["duplicate_materials"]["count"] == 1


def test_the_markdown_report_lists_every_section_and_no_score(tmp_path: Path) -> None:
    box = trimesh.creation.box()
    box.update_faces(np.arange(len(box.faces)) >= 2)
    report = _report_from_file(tmp_path, _scene(box))
    text = ins.markdown(report, "Box")
    assert text.startswith("# Model health report: Box")
    assert "## Topology" in text
    assert "Holes and open borders" in text
    assert "no overall score" in text
    json.dumps(report)  # plain data, ready to save


def test_uv_overlaps_on_very_large_meshes_are_reported_as_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ins, "UV_OVERLAP_MAX_FACES", 100)
    report = _report_from_file(tmp_path, _scene(_textured_ball()))
    skipped = {s["code"]: s for s in report["skipped"]}
    assert "Not checked on meshes over 100 triangles" in skipped["uv_overlaps"]["reason"]


def _mixed_sizes() -> trimesh.Trimesh:
    """A fine sphere (small triangles set the grid's cell size) crossing a rotated box
    whose few large triangles span many cells, diagonally."""
    box = trimesh.creation.box(extents=(2, 2, 2))
    box.apply_transform(trimesh.transformations.euler_matrix(0.6, 0.7, 0.2))
    ball = trimesh.creation.icosphere(subdivisions=4, radius=0.4)
    ball.apply_translation(box.vertices[0] * 0.95)  # through a corner region of the box
    return trimesh.util.concatenate([box, ball])


def test_large_triangles_among_small_ones_are_all_searched() -> None:
    mesh = _mixed_sizes()
    model = ins._gather(_scene(mesh))
    ctx = ins._Context(model, ins.Options())
    grid = ctx.get_grid()
    assert grid.sampled > 0  # some box triangles were registered along their surface
    partial = ins.inspect_scene(_scene(mesh)).skipped
    assert [s.code for s in partial] == ["self_intersections_partial"]  # and the report says so
    expected = _brute_force_pairs(model)
    crossing, _, facts = ins._self_intersections(ctx)
    assert expected
    assert facts["crossing_pairs"] == len(expected)
    assert set(crossing.tolist()) == {f for pair in expected for f in pair}


def test_every_thickness_ray_finds_the_far_wall_on_closed_shapes_of_mixed_sizes() -> None:
    box = trimesh.creation.box(extents=(2, 2, 2))
    box.apply_transform(trimesh.transformations.euler_matrix(0.6, 0.7, 0.2))
    ball = trimesh.creation.icosphere(subdivisions=5, radius=0.2)
    ball.apply_translation([5, 0, 0])  # apart: both closed, small triangles set the cell
    report = ins.inspect_scene(_scene(box, ball), ins.Options(printability=True, min_wall_mm=0.01))
    wall = report.measurements["fabrication"]["wall_thickness"]
    assert wall["measured"] == wall["samples"]


def test_wall_thickness_on_a_noisy_box_follows_the_surface_not_the_noise() -> None:
    """Noise tilting triangles by tens of degrees does not send rays out through a
    neighbouring side: the 5th percentile and median match the true thickness. (Much
    stronger noise folds the surface through itself; readings there are short because
    the surface really is folded, and the self-intersections are reported.)"""
    box = trimesh.creation.box(extents=(1, 1, 1))
    vertices, faces = trimesh.remesh.subdivide_to_size(box.vertices, box.faces, max_edge=0.03)
    mesh = trimesh.Trimesh(vertices, faces)
    mesh.merge_vertices()
    rng = np.random.default_rng(7)
    mesh.vertices = mesh.vertices + rng.normal(0, 0.002, mesh.vertices.shape)
    report = ins.inspect_scene(_scene(mesh), ins.Options(printability=True, min_wall_mm=0.5))
    assert not [f for f in report.findings if f.code == "self_intersections"]
    wall = report.measurements["fabrication"]["wall_thickness"]
    assert wall["measured"] > 0.85 * wall["samples"]  # rays near edges may find no far wall
    assert wall["median"] == pytest.approx(1.0, abs=0.02)
    assert wall["p05"] == pytest.approx(1.0, abs=0.05)
