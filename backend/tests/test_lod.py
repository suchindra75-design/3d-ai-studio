"""Levels of detail for viewing large models (D58)."""

import io
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.processing import lod
from ai3d.processing.mesh import load_model_scene
from ai3d.workers import JobRunner
from tests.test_model_versions import MakeImageProject, _generated_model, _save


@pytest.fixture
def small_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Levels for models of a few thousand triangles, so tests run in seconds."""
    monkeypatch.setattr(lod, "LOD_MIN_TRIANGLES", 4_000)
    monkeypatch.setattr(lod, "LOD_MIN_LEVEL_TRIANGLES", 300)
    monkeypatch.setattr(lod, "LOD_MIN_PART_TRIANGLES", 100)


def _bumpy_ball(subdivisions: int) -> trimesh.Trimesh:
    ball = trimesh.creation.icosphere(subdivisions=subdivisions, radius=0.5)
    ball.vertices *= 1 + 0.02 * np.sin(9 * ball.vertices[:, :1]) * np.cos(7 * ball.vertices[:, 1:2])
    return ball


def _model() -> trimesh.Scene:
    """A textured ball, a ball with orange-top/blue-bottom colours moved to x = 2, and a
    small box (too small to simplify)."""
    textured = _bumpy_ball(5)
    textured.unmerge_vertices()
    # Stored as a triangle soup (every triangle its own vertices), as STL-like files
    # are; planar UVs.
    uv = (textured.vertices[:, :2] + 0.5).clip(0, 1)
    image = Image.fromarray(np.random.default_rng(1).integers(0, 255, (2048, 2048, 3), np.uint8))
    textured.visual = trimesh.visual.TextureVisuals(uv=uv, image=image)
    coloured = _bumpy_ball(5)
    top = coloured.vertices[:, 1] > 0
    colours = np.where(top[:, None], [230, 120, 20, 255], [30, 60, 220, 255]).astype(np.uint8)
    coloured.visual = trimesh.visual.ColorVisuals(coloured, vertex_colors=colours)
    box = trimesh.creation.box(extents=(0.2, 0.2, 0.2))
    scene = trimesh.Scene()
    scene.add_geometry(textured, geom_name="Textured", node_name="Textured")
    scene.add_geometry(
        coloured,
        geom_name="Coloured",
        node_name="Coloured",
        transform=trimesh.transformations.translation_matrix([2, 0, 0]),
    )
    scene.add_geometry(box, geom_name="Box", node_name="Box")
    return scene


def _reopen(data: bytes, tmp_path: Path) -> trimesh.Scene:
    path = tmp_path / "level.glb"
    path.write_bytes(data)
    return load_model_scene(path)


def test_level_targets() -> None:
    assert lod.level_targets(400_000) == []
    assert lod.level_targets(1_000_000) == [250_000, 62_500]
    assert lod.level_targets(5_000_000) == [1_250_000, 312_500, 78_125]


@pytest.mark.usefixtures("small_thresholds")
def test_levels_keep_parts_placement_textures_and_colours(tmp_path: Path) -> None:
    scene = _model()
    original = sum(len(g.faces) for g in scene.geometry.values())
    levels = lod.build_levels(scene)
    targets = lod.level_targets(original)
    assert len(targets) == 3
    assert [lv.level for lv in levels] == [1, 2, 3]
    for level, expected in zip(levels, targets, strict=True):
        assert level.triangles == pytest.approx(expected, rel=0.15)
        assert level.mean_shift < 0.02  # vertices stay near the original surface
        reopened = _reopen(level.glb, tmp_path)
        # Same parts, same place.
        assert reopened.extents == pytest.approx(scene.extents, rel=0.02)
        by_node = {
            n: reopened.geometry[reopened.graph[n][1]] for n in reopened.graph.nodes_geometry
        }
        assert set(by_node) == {"Textured", "Coloured", "Box"}
        assert reopened.graph["Coloured"][0][:3, 3] == pytest.approx([2, 0, 0])
        # The small box is copied unchanged.
        assert len(by_node["Box"].faces) == 12
        # The texture is kept (smaller: only shown until the full model loads), and the
        # UVs stay within the image.
        visual = by_node["Textured"].visual
        assert visual.kind == "texture"
        assert visual.material.baseColorTexture.size == (lod.LOD_TEXTURE_MAX,) * 2
        assert visual.uv.min() >= 0
        assert visual.uv.max() <= 1
        # Colours stay where they were.
        ball = by_node["Coloured"]
        colours = np.asarray(ball.visual.vertex_colors)[:, :3].astype(float)
        top = colours[ball.vertices[:, 1] > 0.2].mean(axis=0)
        bottom = colours[ball.vertices[:, 1] < -0.2].mean(axis=0)
        assert top[0] > top[2] + 150
        assert bottom[2] > bottom[0] + 150
        # Normals are written (smooth shading).
        assert by_node["Coloured"].vertex_normals.shape == by_node["Coloured"].vertices.shape


@pytest.mark.usefixtures("small_thresholds")
def test_a_level_that_changes_the_shape_is_dropped() -> None:
    """Hundreds of triangles meeting at the poles make the simplifier fail at high
    ratios; such a level must not be kept."""
    sphere = trimesh.creation.uv_sphere(radius=1.0, count=[400, 400])
    levels = lod.build_levels(trimesh.Scene(sphere))
    targets = lod.level_targets(len(sphere.faces))
    assert 0 < len(levels) < len(targets)
    for level in levels:
        assert level.mean_shift < lod.LOD_MAX_MEAN_SHIFT


def _glb(scene: trimesh.Scene) -> bytes:
    buffer = io.BytesIO()
    scene.export(buffer, file_type="glb")
    return buffer.getvalue()


@pytest.mark.usefixtures("small_thresholds")
def test_lod_api_builds_once_and_serves_the_levels(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject, tmp_path: Path
) -> None:
    model = _generated_model(client, runner, make_image_project)
    first: dict[str, Any] = model["current_version"]
    # The generated model is small: nothing to build.
    small = client.get(f"/api/v1/model-versions/{first['id']}/lod").json()
    assert small["status"] == "not_needed"
    assert client.post(f"/api/v1/model-versions/{first['id']}/lod").json()["levels"] == []

    saved = _save(client, model["id"], first["id"], _glb(_model()))
    assert saved.status_code == 201, saved.text
    version = saved.json()
    url = f"/api/v1/model-versions/{version['id']}/lod"
    assert client.get(url).json()["status"] == "missing"
    built = client.post(url).json()
    assert built["status"] == "ready"
    assert [lv["level"] for lv in built["levels"]] == [1, 2, 3]
    assert built["triangles"] == version["stats"]["triangle_count"]
    for level in built["levels"]:
        response = client.get(level["file_url"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "model/gltf-binary"
        assert len(response.content) == level["size_bytes"]
        reopened = _reopen(response.content, tmp_path)
        assert sum(len(g.faces) for g in reopened.geometry.values()) == level["triangles"]
    # Building again reuses the stored levels.
    again = client.post(url).json()
    assert again["levels"] == built["levels"]
    assert client.get(f"{url}/9/file").status_code == 404
    # The version itself is unchanged.
    current = client.get(f"/api/v1/models/{model['id']}").json()["current_version"]
    assert current["id"] == version["id"]
    assert current["stats"]["triangle_count"] == version["stats"]["triangle_count"]
