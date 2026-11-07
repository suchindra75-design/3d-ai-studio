"""Levels of detail for viewing large models (D58)."""

import io
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.processing import lod
from ai3d.processing.mesh import load_model_scene
from ai3d.services import lod as lod_service
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


def _build(client: TestClient, url: str) -> dict[str, Any]:
    """Start building (POST) and wait for the background build to finish."""
    state: dict[str, Any] = client.post(url).json()
    deadline = time.monotonic() + 120
    while state["status"] == "building" and time.monotonic() < deadline:
        time.sleep(0.05)
        state = client.get(url).json()
    return state


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
    assert _build(client, f"/api/v1/model-versions/{first['id']}/lod")["levels"] == []

    saved = _save(client, model["id"], first["id"], _glb(_model()))
    assert saved.status_code == 201, saved.text
    version = saved.json()
    url = f"/api/v1/model-versions/{version['id']}/lod"
    assert client.get(url).json()["status"] == "missing"
    started = client.post(url).json()
    assert started["status"] == "building"  # returns at once; the work is in the background
    built = _build(client, url)
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


class _Memory:
    def __init__(self, available: int) -> None:
        self.available = available


@pytest.mark.usefixtures("small_thresholds")
def test_levels_wait_for_free_memory_and_failures_change_nothing(
    client: TestClient,
    runner: JobRunner,
    make_image_project: MakeImageProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai3d.processing import mesh as mesh_module  # noqa: PLC0415

    model = _generated_model(client, runner, make_image_project)
    version = _save(client, model["id"], model["current_version"]["id"], _glb(_model())).json()
    url = f"/api/v1/model-versions/{version['id']}/lod"
    original = client.get(version["file_url"]).content

    # Not enough free memory: refused with the numbers, nothing recorded.
    with monkeypatch.context() as patch:
        patch.setattr(mesh_module.psutil, "virtual_memory", lambda: _Memory(1024))
        refused = client.post(url)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "not_enough_memory"
    assert "Preparing simplified views of" in refused.json()["error"]["message"]
    assert client.get(url).json()["status"] == "missing"

    # Building fails part-way: no level files, the version untouched, and it can retry.
    def broken(source: Path) -> list[lod.LodLevel]:
        raise RuntimeError("simplifier crashed")

    with monkeypatch.context() as patch:
        patch.setattr(lod_service, "_levels", broken)
        failed = _build(client, url)
    assert failed["status"] == "failed"
    assert "Preparing simplified views failed (RuntimeError)" in failed["message"]
    assert failed["levels"] == []
    assert client.get(version["file_url"]).content == original
    assert _build(client, url)["status"] == "ready"  # building again retries
    assert client.get(version["file_url"]).content == original


def test_large_meshes_are_simplified_in_pieces_without_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pieces keep their cut edges, so a closed model stays closed and keeps its colours
    and normals; the result matches simplifying it in one go."""
    ball = _bumpy_ball(6)  # 81,920 triangles, closed
    top = ball.vertices[:, 1] > 0
    colours = np.where(top[:, None], [230, 120, 20, 255], [30, 60, 220, 255]).astype(np.uint8)
    ball.visual = trimesh.visual.ColorVisuals(ball, vertex_colors=colours)
    target = len(ball.faces) // 4
    results = {}
    for limit in (10**9, 25_000):  # one piece; four pieces
        monkeypatch.setattr(lod, "LOD_PIECE_TRIANGLES", limit)
        simplified, mapping = lod.simplify_mesh(ball, target)
        welded = trimesh.Trimesh(simplified.vertices.copy(), simplified.faces.copy(), process=False)
        welded.merge_vertices()
        assert welded.is_watertight  # no gaps where the pieces meet
        assert (mapping >= 0).all()
        assert (np.linalg.norm(simplified.vertex_normals, axis=1) > 0.5).all()
        shown = np.asarray(simplified.visual.vertex_colors)[:, :3].astype(float)
        assert shown[simplified.vertices[:, 1] > 0.2].mean(axis=0)[0] > 200  # still orange
        shift = np.linalg.norm(ball.vertices - simplified.vertices[mapping], axis=1).mean()
        results[limit] = (len(simplified.faces), shift)
    (one_count, one_shift), (four_count, four_shift) = results[10**9], results[25_000]
    assert four_count == pytest.approx(one_count, rel=0.01)
    assert four_shift < one_shift * 1.2


@pytest.mark.usefixtures("small_thresholds")
def test_no_level_when_nothing_can_be_simplified() -> None:
    """Triangles that touch only at corners have every edge on a border, which is kept:
    a "level" would be a copy of the model, so none is made."""
    n = 80
    u, v = np.meshgrid(np.arange(n), np.arange(n))
    vertices = np.stack([u.ravel(), np.zeros(n * n), v.ravel()], 1).astype(float)
    index = np.arange(n * n).reshape(n, n)
    # Only the "lower" triangle of each cell: neighbours share corners, never edges.
    faces = np.stack([index[:-1, :-1].ravel(), index[1:, :-1].ravel(), index[:-1, 1:].ravel()], 1)
    scattered = trimesh.Trimesh(vertices, faces, process=False)
    assert len(faces) >= lod.LOD_MIN_TRIANGLES
    assert lod.build_levels(trimesh.Scene(scattered)) == []


def test_the_server_keeps_answering_while_levels_are_built(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    """The simplifier holds Python's interpreter lock for seconds at a time; built in
    the server process it froze every request (connections were reset). It runs in a
    separate process, so requests are answered while it works."""
    big = trimesh.creation.icosphere(subdivisions=8, radius=0.5)  # 1.3 million triangles
    big.vertices *= 1 + 0.02 * np.sin(9 * big.vertices[:, :1])
    model = _generated_model(client, runner, make_image_project)
    version = _save(client, model["id"], model["current_version"]["id"], _glb(trimesh.Scene(big)))
    url = f"/api/v1/model-versions/{version.json()['id']}/lod"
    assert client.post(url).json()["status"] == "building"
    slowest = 0.0
    state = "building"
    deadline = time.monotonic() + 300
    while state == "building" and time.monotonic() < deadline:
        started = time.monotonic()
        state = client.get(url).json()["status"]
        client.get("/api/v1/health")
        slowest = max(slowest, time.monotonic() - started)
        time.sleep(0.05)
    assert state == "ready"
    assert slowest < 0.5, f"a request took {slowest:.2f} s while building"
