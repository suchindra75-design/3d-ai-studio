"""Geometry/texture processing operations and the version-2 quality report."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

from ai3d.models.enums import QualityMode
from ai3d.processing import geometry
from ai3d.processing.geometry import ScaleReference, process
from ai3d.processing.mesh import validate_mesh_file


def _scene(*meshes: trimesh.Trimesh) -> trimesh.Scene:
    scene = trimesh.Scene()
    for n, mesh in enumerate(meshes):
        scene.add_geometry(mesh, geom_name=f"g{n}", node_name=f"n{n}")
    return scene


def _textured_box(size: int = 64) -> trimesh.Trimesh:
    box = trimesh.creation.box()
    box.unmerge_vertices()
    rng = np.random.default_rng(1)
    box.visual = trimesh.visual.TextureVisuals(
        uv=rng.random((len(box.vertices), 2)),
        image=Image.fromarray(rng.integers(0, 255, (size, size, 3), dtype=np.uint8)),
    )
    return box


def _faces(scene: trimesh.Scene) -> int:
    return sum(len(g.faces) for g in scene.geometry.values())


def test_cleanup_removes_degenerate_and_duplicate_triangles() -> None:
    box = trimesh.creation.box()
    faces = np.vstack([box.faces, box.faces[:2], [[0, 0, 1]]])
    scene = _scene(trimesh.Trimesh(box.vertices, faces, process=False))
    step = geometry.cleanup(scene)
    assert step.status == "applied"
    assert "3 degenerate/duplicate" in step.detail
    assert _faces(scene) == 12
    assert geometry.cleanup(scene).status == "skipped"


def test_normals_are_fixed_on_textured_meshes_without_breaking_uvs() -> None:
    box = _textured_box()
    uv_before = np.asarray(box.visual.uv).copy()
    faces = box.faces.copy()
    faces[:4] = faces[:4][:, ::-1]
    box.faces = faces
    scene = _scene(box)
    step = geometry.fix_normals(scene)
    assert step.status == "applied"
    assert "Flipped 4" in step.detail
    shape = geometry._shape(scene.geometry["g0"])
    assert shape.is_winding_consistent
    assert shape.volume > 0
    assert np.array_equal(scene.geometry["g0"].visual.uv, uv_before)  # texture mapping intact


def test_small_loose_parts_are_removed_but_never_the_whole_model() -> None:
    body = trimesh.creation.icosphere(subdivisions=4)  # 5120 triangles
    crumb = trimesh.creation.box(extents=(0.01, 0.01, 0.01))
    crumb.apply_translation([3, 0, 0])
    scene = _scene(trimesh.util.concatenate([body, crumb]))
    step = geometry.remove_small_parts(scene, 0.005)
    assert step.status == "applied"
    assert "1 piece(s)" in step.detail
    assert _faces(scene) == 5120
    lone = _scene(trimesh.creation.box(extents=(0.01, 0.01, 0.01)))
    assert geometry.remove_small_parts(lone, 0.5).status == "skipped"
    assert _faces(lone) == 12


def test_holes_are_filled_only_on_untextured_meshes() -> None:
    box = trimesh.creation.box()
    open_box = trimesh.Trimesh(box.vertices, box.faces[:-1], process=False)
    plain = _scene(open_box)
    assert geometry.fill_holes(plain).status == "applied"
    assert next(iter(plain.geometry.values())).is_watertight

    textured = _textured_box()
    textured.update_faces(np.arange(len(textured.faces)) != 0)
    scene = _scene(textured)
    step = geometry.fill_holes(scene)
    assert step.status == "skipped"
    assert "would have no texture" in step.detail
    assert _faces(scene) == 11


def test_textures_are_downscaled_to_the_profile_limit() -> None:
    scene = _scene(_textured_box(size=300))
    step = geometry.limit_textures(scene, 128)
    assert step.status == "applied"
    assert "300×300 → 128×128" in step.detail
    assert geometry.limit_textures(scene, 128).status == "skipped"


def test_ground_plane_is_removed_and_the_object_kept() -> None:
    floor = trimesh.creation.box(extents=(4.0, 0.001, 4.0))
    floor = trimesh.Trimesh(*trimesh.remesh.subdivide_to_size(floor.vertices, floor.faces, 0.2))
    floor.apply_translation([0, -0.0005, 0])
    cup = trimesh.creation.cylinder(radius=0.3, height=0.6, sections=32)
    cup.apply_translation([0, 0.3, 0])
    scene = _scene(trimesh.util.concatenate([floor, cup]))
    step = geometry.remove_ground(scene)
    assert step.status == "applied", step.detail
    assert scene.extents == pytest.approx([0.6, 0.6, 0.6], rel=0.05)


def test_no_ground_plane_means_nothing_is_removed() -> None:
    scene = _scene(trimesh.creation.icosphere())
    assert geometry.remove_ground(scene).status == "skipped"
    assert _faces(scene) == 1280


def test_scale_from_a_measurement() -> None:
    scene = _scene(trimesh.creation.box(extents=(2.0, 4.0, 1.0)))
    step, info = geometry.apply_scale(scene, ScaleReference("y", 120))
    assert scene.extents == pytest.approx([0.06, 0.12, 0.03])
    assert info == {
        "known": True,
        "axis": "y",
        "length_mm": 120,
        "factor": pytest.approx(0.03),
        "source": "user measurement",
    }
    assert "only as accurate as that measurement" in step.detail
    _, longest = geometry.apply_scale(scene, ScaleReference("longest", 50))
    assert longest["axis"] == "y"


def test_professional_decimates_untextured_but_not_textured_models() -> None:
    dense = _scene(trimesh.creation.icosphere(subdivisions=5))  # 20480 triangles
    step = geometry.decimate(dense, 5000)
    assert step.status == "applied"
    assert _faces(dense) <= 5000
    textured = _scene(_textured_box())
    assert geometry.decimate(textured, 4).status == "skipped"


def test_profiles_report_tools_honestly() -> None:
    result = process(_scene(trimesh.creation.box()), QualityMode.PROFESSIONAL)
    by_code = {s.code: s for s in result.steps}
    assert by_code["retopology"].status == "unavailable"
    assert "QuadriFlow" in by_code["retopology"].detail
    assert by_code["baking"].status == "skipped"
    assert by_code["baking"].detail.startswith("Not needed: the model is within 300,000")
    fast = [s.code for s in process(_scene(trimesh.creation.box()), QualityMode.FAST).steps]
    assert fast == ["cleanup", "small_parts", "textures"]
    assert result.changed is False


def _small_professional(monkeypatch: pytest.MonkeyPatch, budget: int) -> None:
    pro = geometry.PROFILES[QualityMode.PROFESSIONAL]
    monkeypatch.setitem(
        geometry.PROFILES,
        QualityMode.PROFESSIONAL,
        geometry.Profile(
            pro.min_part_fraction, pro.fix_normals, pro.fill_holes, pro.max_texture, budget, True
        ),
    )


def test_without_blender_textured_models_over_the_budget_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _small_professional(monkeypatch, 4)
    reason = "Needs Blender 4.2 or newer (blender.org), which was not found on this computer."
    monkeypatch.setattr(geometry, "check_blender", lambda: reason)
    scene = _scene(_textured_box())
    result = process(scene, QualityMode.PROFESSIONAL)
    by_code = {s.code: s for s in result.steps}
    assert by_code["decimate"].status == "skipped"
    assert by_code["baking"].status == "unavailable"
    assert by_code["baking"].detail.startswith(reason)
    assert result.scene is scene
    assert _faces(scene) == 12


def test_untextured_models_over_the_budget_need_no_baking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _small_professional(monkeypatch, 5000)
    result = process(_scene(trimesh.creation.icosphere(subdivisions=5)), QualityMode.PROFESSIONAL)
    by_code = {s.code: s for s in result.steps}
    assert by_code["decimate"].status == "applied"
    assert by_code["baking"].status == "skipped"
    assert "no texture" in by_code["baking"].detail
    assert _faces(result.scene) <= 5000


def test_bake_texture_size_follows_the_original_within_limits() -> None:
    small = _scene(_textured_box(64))
    assert geometry.bake_texture_size(small, 8192) == 1024
    assert geometry.bake_texture_size(_scene(_textured_box(2000)), 8192) == 2048
    assert geometry.bake_texture_size(_scene(_textured_box(6000)), 8192) == 4096
    assert geometry.bake_texture_size(_scene(_textured_box(6000)), 2048) == 2048


# -- quality report v2 ------------------------------------------------------------------


def _report(tmp_path: Path, mesh: trimesh.Trimesh) -> dict:
    path = tmp_path / "m.glb"
    path.write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
    report = validate_mesh_file(path)
    return {"metrics": report.metrics, "codes": {i.code for i in report.issues}}


def test_report_finds_non_manifold_edges(tmp_path: Path) -> None:
    box = trimesh.creation.box()
    # A fin sharing one edge with the box: that edge now has three triangles.
    a, b = box.edges_unique[0]
    vertices = np.vstack([box.vertices, [[2.0, 2.0, 2.0]]])
    faces = np.vstack([box.faces, [[a, b, len(box.vertices)]]])
    found = _report(tmp_path, trimesh.Trimesh(vertices, faces, process=False))
    assert found["metrics"]["non_manifold_edges"] == 1
    assert found["metrics"]["winding_consistent"] is None  # undefined when non-manifold
    assert "non_manifold" in found["codes"]


def test_report_finds_inconsistent_winding_and_open_edges(tmp_path: Path) -> None:
    box = trimesh.creation.box()
    faces = box.faces.copy()
    faces[0] = faces[0][::-1]
    found = _report(
        tmp_path, trimesh.Trimesh(box.vertices, [*faces[1:].tolist(), faces[0]], process=False)
    )
    assert found["metrics"]["winding_consistent"] is False
    assert "inconsistent_winding" in found["codes"]
    open_box = _report(tmp_path, trimesh.Trimesh(box.vertices, box.faces[1:], process=False))
    assert open_box["metrics"]["boundary_edges"] == 3
    assert open_box["metrics"]["volume"] is None


def test_report_texture_facts(tmp_path: Path) -> None:
    box = _textured_box(size=32)
    box.visual.uv = np.asarray(box.visual.uv) * 2  # about 3/4 of the coordinates exceed 1
    found = _report(tmp_path, box)
    assert found["metrics"]["texture_sizes"] == [[32, 32]]
    assert found["metrics"]["uv_out_of_range"] > 0.5
    assert "uv_out_of_range" in found["codes"]
    closed = _report(tmp_path, trimesh.creation.box())
    assert closed["metrics"]["volume"] == pytest.approx(1.0)
    assert closed["metrics"]["boundary_edges"] == 0


def test_only_blender_baking_waits_for_the_heavy_work_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    @contextmanager
    def turn() -> Iterator[None]:
        order.append("wait")
        yield
        order.append("release")

    def fake_bake(scene: trimesh.Scene, *_args: object, **_kw: object) -> object:
        order.append("bake")
        skipped = geometry.Step("decimate", "Reduce", "skipped", "fake")
        return None, skipped, geometry.Step("baking", "Texture baking", "skipped", "fake")

    _small_professional(monkeypatch, 4)
    monkeypatch.setattr(geometry, "check_blender", lambda: None)
    monkeypatch.setattr(geometry, "bake_reduce", fake_bake)
    process(_scene(_textured_box()), QualityMode.PROFESSIONAL, heavy_turn=turn)
    assert order == ["wait", "bake", "release"]
    order.clear()
    process(_scene(trimesh.creation.box()), QualityMode.PROFESSIONAL, heavy_turn=turn)
    assert order == []  # nothing to bake: no waiting


def test_an_inside_out_model_is_reported_and_enhance_turns_it_round(tmp_path: Path) -> None:
    box = trimesh.creation.box()
    box.invert()
    path = tmp_path / "inside_out.glb"
    path.write_bytes(_scene(box).export(file_type="glb"))
    report = validate_mesh_file(path)
    assert report.metrics["inside_out"] is True
    assert "inside_out" in [i.code for i in report.issues]

    result = process(_scene(box), QualityMode.HIGH_QUALITY)
    normals = next(s for s in result.steps if s.code == "normals")
    assert normals.status == "applied"
    fixed = tmp_path / "fixed.glb"
    fixed.write_bytes(result.scene.export(file_type="glb"))
    report = validate_mesh_file(fixed)
    assert report.metrics["inside_out"] is False
    assert "inside_out" not in [i.code for i in report.issues]
