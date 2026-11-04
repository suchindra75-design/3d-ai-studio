"""Features that run Blender: FBX export (re-opened in a separate Blender run) and
texture baking. Skipped when Blender is not installed."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.models.enums import ExportFormat, QualityMode
from ai3d.processing import exporters, geometry
from ai3d.processing.exporters import ExportError
from ai3d.processing.geometry import PROFILES, Profile, process
from ai3d.workers import JobRunner
from ai3d_engine.blender import check_blender
from tests.test_model_versions import _generated_model, _save

BLENDER_PROBLEM = check_blender()
pytestmark = pytest.mark.skipif(
    BLENDER_PROBLEM is not None, reason=f"Blender is not available: {BLENDER_PROBLEM}"
)

MakeImageProject = Callable[..., tuple[dict[str, Any], list[str]]]
RADIUS = 0.3


def _sphere_colour(directions: np.ndarray, texture: np.ndarray) -> np.ndarray:
    """The texture colour the test sphere shows in each (unit) direction."""
    u = (np.arctan2(directions[:, 0], directions[:, 2]) / (2 * np.pi)) % 1.0
    v = 1 - np.arccos(np.clip(directions[:, 1], -1, 1)) / np.pi
    return _sample(texture, np.stack([u, v], axis=1))


def _sample(texture: np.ndarray, uv: np.ndarray) -> np.ndarray:
    h, w = texture.shape[:2]
    x = np.clip(((uv[:, 0] % 1.0) * w).astype(int), 0, w - 1)
    y = np.clip(((1 - uv[:, 1]) * h).astype(int), 0, h - 1)
    return np.asarray(texture[y, x], dtype=float)


def _texture(size: int = 256) -> np.ndarray:
    """Smooth colours that wrap around in u, so nearby points have nearby colours."""
    yy, xx = np.mgrid[0:size, 0:size] / size
    rgb = np.stack([0.5 + 0.5 * np.cos(2 * np.pi * xx), yy, np.full_like(xx, 0.3)], axis=-1)
    return np.asarray((rgb * 255).astype(np.uint8))


def _textured_sphere(subdivisions: int, texture: np.ndarray) -> trimesh.Trimesh:
    """Icosphere with spherical UVs (seam faces unwrapped past u = 1)."""
    mesh = trimesh.creation.icosphere(subdivisions=subdivisions, radius=RADIUS)
    mesh.unmerge_vertices()
    d = mesh.vertices / RADIUS
    u = (np.arctan2(d[:, 0], d[:, 2]) / (2 * np.pi)) % 1.0
    v = 1 - np.arccos(np.clip(d[:, 1], -1, 1)) / np.pi
    uv = np.stack([u, v], axis=1).reshape(-1, 3, 2)
    seam = (uv[:, :, 0].max(axis=1) - uv[:, :, 0].min(axis=1)) > 0.5
    uv[seam, :, 0] = np.where(uv[seam, :, 0] < 0.5, uv[seam, :, 0] + 1, uv[seam, :, 0])
    mesh.visual = trimesh.visual.TextureVisuals(
        uv=uv.reshape(-1, 2),
        material=trimesh.visual.material.PBRMaterial(
            baseColorTexture=Image.fromarray(texture),
            baseColorFactor=[255, 255, 255, 255],
            metallicFactor=0.0,
            roughnessFactor=0.7,
        ),
    )
    return mesh


def _tinted_box() -> trimesh.Trimesh:
    """Textured box whose material multiplies the texture by 0.4 (trimesh's default)."""
    box = trimesh.creation.box(extents=(0.2, 0.5, 0.1))
    box.unmerge_vertices()
    rng = np.random.default_rng(1)
    box.visual = trimesh.visual.TextureVisuals(
        uv=rng.random((len(box.vertices), 2)),
        image=Image.fromarray(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)),
    )
    return box


def _scene(*meshes: trimesh.Trimesh, offset: float = 0.0) -> trimesh.Scene:
    scene = trimesh.Scene()
    for n, mesh in enumerate(meshes):
        transform = trimesh.transformations.translation_matrix((offset * n, 0.0, 0.0))
        scene.add_geometry(mesh, geom_name=f"g{n}", node_name=f"n{n}", transform=transform)
    return scene


def _write_fbx(scene: trimesh.Scene, dest: Path) -> list[str]:
    return exporters.write(scene, ExportFormat.FBX, dest)


# -- FBX ----------------------------------------------------------------------------------


def test_fbx_is_available_with_blender() -> None:
    info = exporters.format_info(ExportFormat.FBX)
    assert info.available is True
    assert info.reason is None
    assert info.keeps_texture is True


def test_fbx_export_of_a_textured_model_reopens_with_same_triangles_size_and_texture(
    tmp_path: Path,
) -> None:
    scene = _scene(_textured_sphere(3, _texture()), _tinted_box(), offset=1.0)
    facts = exporters.scene_facts(scene)
    assert facts.textured
    out = tmp_path / "model.fbx"
    notes = _write_fbx(scene, out)

    data = out.read_bytes()
    assert data.startswith(b"Kaydara FBX Binary  \x00")
    assert data.count(b"\x89PNG\r\n\x1a\n") == 2  # both textures embedded in the file
    # The box's 0.4 tint cannot be expressed in FBX, and the export says so.
    assert len(notes) == 1
    assert "colour factor (0.4, 0.4, 0.4)" in notes[0]

    validation = exporters.validate(out, ExportFormat.FBX, facts)
    assert validation["reopened_with"].startswith("Blender 4.")
    assert validation["reopened_with"].endswith("FBX importer")
    assert validation["triangles"] == facts.triangles == 1280 + 12
    assert validation["size"] == pytest.approx(facts.size, rel=1e-4)
    assert validation["size"][0] == pytest.approx(1.0 + RADIUS + 0.1, rel=1e-4)  # metres
    assert validation["textured"] is True


def test_a_broken_or_different_fbx_is_rejected(tmp_path: Path) -> None:
    textured = _scene(_textured_sphere(3, _texture()))
    facts = exporters.scene_facts(textured)

    not_fbx = tmp_path / "not.fbx"
    not_fbx.write_bytes(b"this is not an fbx file")
    with pytest.raises(ExportError, match="not a binary FBX"):
        exporters.validate(not_fbx, ExportFormat.FBX, facts)

    good = tmp_path / "good.fbx"
    _write_fbx(textured, good)
    truncated = tmp_path / "truncated.fbx"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 3])
    with pytest.raises(ExportError, match="could not re-open the FBX"):
        exporters.validate(truncated, ExportFormat.FBX, facts)

    other = tmp_path / "box.fbx"
    _write_fbx(_scene(trimesh.creation.box()), other)
    with pytest.raises(ExportError, match="does not match the model") as excinfo:
        exporters.validate(other, ExportFormat.FBX, facts)
    assert "12 triangles instead of 1,280" in str(excinfo.value)
    assert "the texture is missing" in str(excinfo.value)

    # Same geometry, but one of the two meshes lost its texture.
    two = _scene(_textured_sphere(3, _texture()), _tinted_box(), offset=1.0)
    plain_box = trimesh.creation.box(extents=(0.2, 0.5, 0.1))
    one = tmp_path / "one-texture.fbx"
    _write_fbx(_scene(_textured_sphere(3, _texture()), plain_box, offset=1.0), one)
    with pytest.raises(ExportError, match="only 1 of 2 textured meshes have their texture"):
        exporters.validate(one, ExportFormat.FBX, exporters.scene_facts(two))


def test_fbx_export_through_the_api_with_real_scale(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    model = _generated_model(client, runner, make_image_project)
    version = model["current_version"]
    textured = _scene(_textured_sphere(3, _texture())).export(file_type="glb")
    saved = _save(client, model["id"], version["id"], textured).json()
    export = client.post(
        f"/api/v1/model-versions/{saved['id']}/exports", json={"format": "fbx"}
    ).json()
    assert export["status"] == "ready", export["error_message"]
    assert export["validation"]["triangles"] == 1280
    assert export["validation"]["textured"] is True
    assert export["validation"]["size"] == pytest.approx(saved["stats"]["bbox_size"], rel=1e-3)
    download = client.get(export["file_url"])
    assert download.headers["content-disposition"].endswith('.fbx"')
    assert download.content.startswith(b"Kaydara FBX Binary")


# -- baking -------------------------------------------------------------------------------


def _baked_colour_error(source_texture: np.ndarray, baked: trimesh.Scene) -> np.ndarray:
    """Per-sample colour difference (0–1) between the baked texture and the original."""
    (mesh,) = [g for g in baked.geometry.values() if isinstance(g, trimesh.Trimesh)]
    points, faces = trimesh.sample.sample_surface(mesh, 3000, seed=0)
    corners = mesh.vertices[mesh.faces[faces]]
    bary = trimesh.triangles.points_to_barycentric(corners, points)
    uv = (mesh.visual.uv[mesh.faces[faces]] * bary[:, :, None]).sum(axis=1)
    baked_texture = np.asarray(mesh.visual.material.baseColorTexture.convert("RGB"))
    got = _sample(baked_texture, uv)
    directions = points / np.linalg.norm(points, axis=1, keepdims=True)
    want = _sphere_colour(directions, source_texture)
    return np.asarray(np.abs(got - want).max(axis=1) / 255.0)


def test_bake_reduce_keeps_the_look_of_a_textured_model(tmp_path: Path) -> None:
    texture = _texture()
    scene = _scene(_textured_sphere(4, texture))  # 5,120 triangles
    baked, reduce_step, bake_step = geometry.bake_reduce(scene, 500, 512, workdir=tmp_path)

    assert baked is not None
    assert reduce_step.status == "applied"
    assert reduce_step.detail.startswith("5,120 → 500 triangles in Blender")
    assert bake_step.status == "applied"
    assert "512×512 px" in bake_step.detail
    (mesh,) = baked.geometry.values()
    assert len(mesh.faces) == 500
    assert mesh.visual.kind == "texture"
    assert mesh.visual.material.baseColorTexture.size == (512, 512)
    assert baked.extents == pytest.approx(scene.extents, rel=0.01)
    error = _baked_colour_error(texture, baked)
    # Typical error is one or two 8-bit steps; the poles (where the original's own
    # spherical UVs pinch together) are the only places that differ more.
    assert np.median(error) <= 2 / 255
    assert np.percentile(error, 95) <= 4 / 255
    assert list(tmp_path.iterdir()) == []  # temporary files are removed


def test_bake_failure_is_reported_and_the_model_kept(tmp_path: Path) -> None:
    untextured = _scene(trimesh.creation.icosphere(subdivisions=3))
    baked, reduce_step, bake_step = geometry.bake_reduce(untextured, 100, 512, workdir=tmp_path)
    assert baked is None
    assert reduce_step.status == bake_step.status == "skipped"
    assert "no image texture to bake from" in bake_step.detail
    assert len(untextured.geometry["g0"].faces) == 1280


@pytest.fixture
def small_budget(monkeypatch: pytest.MonkeyPatch) -> int:
    """Professional with a 600-triangle budget, so baking runs on a small model."""
    budget = 600
    professional = PROFILES[QualityMode.PROFESSIONAL]
    monkeypatch.setitem(
        PROFILES,
        QualityMode.PROFESSIONAL,
        Profile(
            professional.min_part_fraction,
            professional.fix_normals,
            professional.fill_holes,
            professional.max_texture,
            budget,
            professional.report_tools,
        ),
    )
    return budget


def test_professional_enhance_bakes_a_textured_model_over_the_budget(
    client: TestClient,
    runner: JobRunner,
    make_image_project: MakeImageProject,
    small_budget: int,
) -> None:
    model = _generated_model(client, runner, make_image_project)
    textured = _scene(_textured_sphere(4, _texture())).export(file_type="glb")
    source = _save(client, model["id"], model["current_version"]["id"], textured).json()

    plan = client.post(
        f"/api/v1/models/{model['id']}/enhance/plan", json={"quality_mode": "professional"}
    ).json()
    steps = {s["code"]: s for s in plan["steps"]}
    assert steps["baking"]["status"] == "will_run"
    assert steps["retopology"]["status"] == "unavailable"

    job = client.post(
        f"/api/v1/models/{model['id']}/enhance",
        json={"quality_mode": "professional", "scale": {"axis": "y", "length_mm": 120}},
    ).json()
    assert runner.run_until_idle() == 1
    final = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert final["status"] == "succeeded", final["error"]

    current = client.get(f"/api/v1/models/{model['id']}").json()["current_version"]
    assert current["id"] == final["result_model_version_id"]
    assert current["parent_version_id"] == source["id"]
    assert current["stats"]["triangle_count"] <= small_budget
    assert current["stats"]["bbox_size"][1] == pytest.approx(0.12, rel=1e-4)  # scaled after
    report = {s["code"]: s for s in current["quality_report"]["metrics"]["enhancement"]}
    assert report["decimate"]["status"] == "applied"
    assert report["baking"]["status"] == "applied"
    assert report["retopology"]["status"] == "unavailable"
    reopened = trimesh.load_scene(
        trimesh.util.wrap_as_stream(client.get(current["file_url"]).content), file_type="glb"
    )
    (mesh,) = reopened.geometry.values()
    assert mesh.visual.kind == "texture"
    # The source texture is 256 px; baking uses at least 1024 px.
    assert mesh.visual.material.baseColorTexture.size == (1024, 1024)


@pytest.mark.slow
def test_professional_bakes_a_model_just_over_the_real_budget() -> None:
    scene = _scene(_textured_sphere(7, _texture()))  # 327,680 triangles
    result = process(scene, QualityMode.PROFESSIONAL)
    steps = {s.code: s for s in result.steps}
    assert steps["decimate"].status == "applied"
    assert steps["decimate"].detail.startswith("327,680 → 300,000 triangles")
    assert steps["baking"].status == "applied"
    assert result.scene is not scene
    (mesh,) = result.scene.geometry.values()
    assert len(mesh.faces) == 300_000
    assert mesh.visual.kind == "texture"
    assert result.scene.extents == pytest.approx(scene.extents, rel=1e-3)
