"""Repairs (processing/repair.py) and the inspection and repair API."""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import update

from ai3d.core.timeutil import utcnow
from ai3d.models import GenerationJob
from ai3d.processing import inspect as ins
from ai3d.processing import repair
from ai3d.workers import JobRunner
from tests.test_model_versions import _generated_model, _save

MakeImageProject = Callable[..., tuple[dict[str, Any], list[str]]]


def _scene(*meshes: trimesh.Trimesh) -> trimesh.Scene:
    scene = trimesh.Scene()
    for i, mesh in enumerate(meshes):
        scene.add_geometry(mesh, geom_name=f"part{i}", node_name=f"part{i}")
    return scene


def _codes(scene: trimesh.Scene) -> dict[str, ins.Finding]:
    report = ins.inspect_scene(scene)
    return {f.code: f for f in report.findings}


def _textured(mesh: trimesh.Trimesh, seed: int = 0) -> trimesh.Trimesh:
    rng = np.random.default_rng(seed)
    image = Image.fromarray(rng.integers(0, 255, (16, 16, 3), dtype=np.uint8))
    mesh.visual = trimesh.visual.TextureVisuals(uv=rng.random((len(mesh.vertices), 2)), image=image)
    return mesh


def test_removing_degenerate_and_duplicate_triangles_keeps_the_rest_unchanged() -> None:
    box = trimesh.creation.box()
    extra = [[0, 0, 1], box.faces[3]]
    mesh = _textured(trimesh.Trimesh(box.vertices, np.vstack([box.faces, extra]), process=False))
    uv_of_corner = {
        tuple(mesh.vertices[v]): tuple(mesh.visual.uv[v]) for v in range(len(mesh.vertices))
    }
    scene = _scene(mesh)
    first = repair.remove_degenerate_faces(scene)
    second = repair.remove_duplicate_faces(scene)
    assert first.status == "applied"
    assert "Removed 1 triangles" in first.detail
    assert second.status == "applied"
    assert "Removed 1 triangles" in second.detail
    (fixed,) = scene.geometry.values()
    assert len(fixed.faces) == 12
    for v in range(len(fixed.vertices)):  # every remaining corner keeps its UV
        assert tuple(fixed.visual.uv[v]) == uv_of_corner[tuple(fixed.vertices[v])]
    codes = _codes(scene)
    assert "degenerate_faces" not in codes
    assert "duplicate_faces" not in codes
    assert repair.remove_duplicate_faces(scene).status == "skipped"


def test_fix_winding_and_recalculate_normals() -> None:
    ball = trimesh.creation.icosphere(subdivisions=2)
    faces = ball.faces.copy()
    faces[:5] = faces[:5, ::-1]
    scene = _scene(trimesh.Trimesh(ball.vertices, faces, process=False))
    assert "inconsistent_winding" in _codes(scene)
    step = repair.run_repairs(scene, [{"code": "fix_winding"}]).steps[0]
    assert step.code == "fix_winding"
    assert step.status == "applied"
    assert "inconsistent_winding" not in _codes(scene)
    # Stored normals pointing inwards are replaced with ones from the triangles.
    mesh = trimesh.creation.icosphere(subdivisions=2)
    mesh._cache["vertex_normals"] = -mesh.vertex_normals
    scene = _scene(mesh)
    assert "normals_against_faces" in _codes(scene)
    before = mesh.vertices.copy()
    step = repair.recalculate_normals(scene)
    assert step.status == "applied"
    assert f"{len(mesh.vertices):,} differ" in step.detail
    assert "normals_against_faces" not in _codes(scene)
    assert np.array_equal(scene.geometry["part0"].vertices, before)  # nothing moved


def test_merging_duplicates_keeps_seams() -> None:
    plain = trimesh.creation.icosphere(subdivisions=2)
    plain.unmerge_vertices()
    scene = _scene(plain)
    step = repair.merge_duplicate_vertices(scene)
    assert step.status == "applied"
    assert len(scene.geometry["part0"].vertices) == 162
    seams = _textured(trimesh.creation.icosphere(subdivisions=2))
    seams.unmerge_vertices()  # each copy now has its own (random) UV: real seams
    seams.visual.uv = np.random.default_rng(1).random((len(seams.vertices), 2))
    scene = _scene(seams)
    assert repair.merge_duplicate_vertices(scene).status == "skipped"
    assert len(scene.geometry["part0"].vertices) == 960


def _with_hand_set_normals(*, open_side: bool = False) -> tuple[trimesh.Scene, dict[Any, Any]]:
    """Two boxes in one mesh with unusual stored normals, plus a duplicate triangle, a
    zero-area one and a duplicated vertex for the repairs to remove (or, with
    ``open_side``, one side of the second box missing: a hole to fill)."""
    box = trimesh.creation.box()
    far = trimesh.creation.box()
    far.apply_translation([3, 0, 0])
    rng = np.random.default_rng(2)
    normals = rng.normal(size=(16, 3))
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    vertices = np.vstack([box.vertices, far.vertices, box.vertices[:1]])
    normals = np.vstack([normals, normals[:1]])
    if open_side:
        faces = np.vstack([box.faces, far.faces[2:] + 8, [[16, 1, 2]]])
    else:
        extra = [box.faces[0], [0, 0, 1], [16, 1, 2]]
        faces = np.vstack([box.faces, far.faces + 8, extra])
    mesh = trimesh.Trimesh(vertices, faces, vertex_normals=normals, process=False)
    by_position = {tuple(v): n for v, n in zip(vertices, normals, strict=True)}
    return _scene(mesh), by_position


@pytest.mark.parametrize(
    "operation",
    [
        {"code": "remove_degenerate_faces"},
        {"code": "remove_duplicate_faces"},
        {"code": "merge_duplicate_vertices"},
        {"code": "repair_non_manifold"},
        {"code": "fix_winding"},
        {"code": "separate_components"},
        {"code": "remove_components", "pieces": [1]},
        {"code": "fill_small_holes"},
    ],
    ids=lambda op: op["code"],
)
def test_repairs_keep_the_stored_normals(operation: dict[str, Any]) -> None:
    # trimesh drops stored normals whenever faces change; a repair must not (they carry
    # hand-set hard edges). Checked after a GLB round trip, as the new version is saved.
    filling = operation["code"] == "fill_small_holes"
    scene, by_position = _with_hand_set_normals(open_side=filling)
    result = repair.run_repairs(scene, [operation])
    assert result.steps[0].status == "applied" if filling else True
    saved = trimesh.load_scene(
        io.BytesIO(scene.export(file_type="glb")), file_type="glb", process=False
    )
    checked = 0
    for mesh in saved.geometry.values():
        assert not mesh.vertex_attributes  # nothing extra is written to the file
        stored = mesh._cache.cache.get("vertex_normals")
        assert stored is not None
        for vertex, normal in zip(mesh.vertices, stored, strict=True):
            np.testing.assert_allclose(normal, by_position[tuple(vertex)], atol=1e-6)
            checked += 1
    assert checked >= 8


def test_filling_small_holes_only_on_plain_meshes() -> None:
    box = trimesh.creation.box()
    box.update_faces(np.arange(len(box.faces)) >= 2)
    scene = _scene(box)
    step = repair.fill_small_holes(scene)
    assert step.status == "applied"
    assert "Only openings of three or four edges" in step.detail
    assert ins.inspect_scene(scene).measurements["topology"]["watertight"] is True
    textured = _textured(trimesh.creation.box())
    textured.update_faces(np.arange(len(textured.faces)) >= 2)
    scene = _scene(textured)
    step = repair.fill_small_holes(scene)
    assert step.status == "skipped"
    assert "textured or coloured" in step.detail


def _two_pieces() -> trimesh.Trimesh:
    small = trimesh.creation.box(extents=(0.1, 0.1, 0.1))
    small.apply_translation([3, 0, 0])
    return trimesh.util.concatenate([trimesh.creation.icosphere(subdivisions=2), small])


def test_separating_and_removing_chosen_pieces() -> None:
    scene = _scene(_two_pieces())
    step = repair.separate_components(scene)
    assert step.status == "applied"
    assert len(scene.geometry) == 2
    assert sum(len(m.faces) for m in scene.geometry.values()) == 320 + 12
    scene = _scene(_two_pieces())
    pieces = _codes(scene)["components"].details["pieces"]
    small_id = pieces[1]["id"]
    step = repair.remove_components(scene, [small_id])
    assert step.status == "applied"
    assert "12 triangles deleted" in step.detail
    assert "components" not in _codes(scene)
    everything = repair.remove_components(_scene(_two_pieces()), [p["id"] for p in pieces])
    assert everything.status == "skipped"
    assert "every piece" in everything.detail


def test_non_manifold_repair_fixes_duplicates_and_reports_the_rest() -> None:
    box = trimesh.creation.box()
    doubled = trimesh.Trimesh(box.vertices, np.vstack([box.faces, box.faces[:1]]), process=False)
    scene = _scene(doubled)
    assert _codes(scene)["non_manifold_edges"].count == 3
    step = repair.repair_non_manifold(scene)
    assert step.status == "applied"
    assert "3 → 0" in step.detail
    a, b = box.faces[0][:2]
    fin = trimesh.Trimesh(
        np.vstack([box.vertices, [[0, 2, 0]]]),
        np.vstack([box.faces, [[a, b, len(box.vertices)]]]),
        process=False,
    )
    step = repair.repair_non_manifold(_scene(fin))
    assert step.status == "skipped"
    assert "left for manual editing" in step.detail


# -- through the API ---------------------------------------------------------------------


def _glb(scene: trimesh.Scene) -> bytes:
    buffer = io.BytesIO()
    scene.export(buffer, file_type="glb")
    return buffer.getvalue()


def _broken_version(client: TestClient, model: dict[str, Any]) -> dict[str, Any]:
    box = trimesh.creation.box()
    mesh = trimesh.Trimesh(box.vertices, np.vstack([box.faces, box.faces[:2]]), process=False)
    response = _save(client, model["id"], model["current_version"]["id"], _glb(_scene(mesh)))
    assert response.status_code == 201, response.text
    version: dict[str, Any] = response.json()
    return version


def _inspect(client: TestClient, version_id: str, **body: Any) -> dict[str, Any]:
    url = f"/api/v1/model-versions/{version_id}/inspection"
    state: dict[str, Any] = client.post(url, json=body).json()
    deadline = time.monotonic() + 120
    while state["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        state = client.get(url).json()
    return state


def test_inspection_api_runs_in_the_background_and_offers_downloads(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    model = _generated_model(client, runner, make_image_project)
    version = _broken_version(client, model)
    url = f"/api/v1/model-versions/{version['id']}/inspection"
    assert client.get(url).json()["status"] == "missing"
    state = _inspect(client, version["id"], printability=True)
    assert state["status"] == "ready", state
    codes = {f["code"]: f for f in state["report"]["findings"]}
    assert codes["duplicate_faces"]["count"] == 2
    assert codes["duplicate_faces"]["repair"] == "remove_duplicate_faces"
    assert state["report"]["options"]["printability"] is True
    as_json = client.get(state["json_url"])
    assert as_json.headers["content-disposition"].startswith("attachment")
    assert as_json.json()["findings"] == state["report"]["findings"]
    text = client.get(state["markdown_url"])
    assert text.headers["content-type"].startswith("text/markdown")
    assert "# Model health report" in text.text
    # Inspecting again replaces the report (one per version).
    again = _inspect(client, version["id"])
    assert again["report"]["options"]["printability"] is False


def test_a_failed_inspection_is_reported_and_changes_nothing(
    client: TestClient,
    runner: JobRunner,
    make_image_project: MakeImageProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai3d.services import inspection as service  # noqa: PLC0415

    model = _generated_model(client, runner, make_image_project)
    version = model["current_version"]
    original = client.get(version["file_url"]).content

    def broken(path: Any, options: Any) -> dict[str, Any]:
        raise RuntimeError("checker crashed")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_inspect_in_process", broken)
        state = _inspect(client, version["id"])
    assert state["status"] == "failed"
    assert "RuntimeError" in state["message"]
    assert client.get(version["file_url"]).content == original

    class Memory:
        available = 1024

    from ai3d.processing import mesh as mesh_module  # noqa: PLC0415

    with monkeypatch.context() as patch:
        patch.setattr(mesh_module.psutil, "virtual_memory", Memory)
        refused = client.post(f"/api/v1/model-versions/{version['id']}/inspection", json={})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "not_enough_memory"


def _repair(client: TestClient, model_id: str, version_id: str, *ops: dict[str, Any]) -> Any:
    return client.post(
        f"/api/v1/models/{model_id}/repairs",
        json={"source_version_id": version_id, "operations": list(ops)},
    )


def test_a_repair_makes_a_new_version_and_the_original_can_be_restored(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    model = _generated_model(client, runner, make_image_project)
    broken = _broken_version(client, model)
    original = client.get(broken["file_url"]).content
    response = _repair(client, model["id"], broken["id"], {"code": "remove_duplicate_faces"})
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["provider_summary"] == ["Repair: Remove duplicate triangles"]
    runner.run_until_idle()
    done = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert done["status"] == "succeeded", done
    fixed_id = done["result_model_version_id"]
    items = client.get(f"/api/v1/models/{model['id']}/versions").json()["items"]
    fixed = next(v for v in items if v["id"] == fixed_id)
    assert fixed["parent_version_id"] == broken["id"]
    steps = fixed["quality_report"]["metrics"]["enhancement"]
    assert steps == [
        {
            "code": "remove_duplicate_faces",
            "title": "Remove duplicate triangles",
            "status": "applied",
            "detail": steps[0]["detail"],
        }
    ]
    assert "Removed 2 triangles" in steps[0]["detail"]
    assert fixed["quality_report"]["metrics"]["triangle_count"] == 12
    assert client.get(broken["file_url"]).content == original  # never changed
    # Undo: make the broken version current again.
    restored = client.patch(
        f"/api/v1/models/{model['id']}", json={"current_version_id": broken["id"]}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["current_version"]["id"] == broken["id"]


def test_removing_pieces_must_name_them(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    model = _generated_model(client, runner, make_image_project)
    version = model["current_version"]
    refused = _repair(client, model["id"], version["id"], {"code": "remove_components"})
    assert refused.status_code == 422
    refused = _repair(client, model["id"], version["id"], {"code": "fix_winding", "pieces": [1]})
    assert refused.status_code == 422


def test_a_failed_repair_saves_nothing_and_an_interrupted_one_runs_once(
    client: TestClient,
    runner: JobRunner,
    make_image_project: MakeImageProject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai3d.workers import enhance as worker  # noqa: PLC0415

    model = _generated_model(client, runner, make_image_project)
    broken = _broken_version(client, model)
    versions_before = len(client.get(f"/api/v1/models/{model['id']}/versions").json()["items"])

    def crash(scene: Any, operations: Any) -> Any:
        raise RuntimeError("repair crashed")

    with monkeypatch.context() as patch:
        patch.setattr(worker, "run_repairs", crash)
        job = _repair(client, model["id"], broken["id"], {"code": "remove_duplicate_faces"}).json()
        runner.run_until_idle()
    failed = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert failed["status"] == "failed"
    assert failed["result_model_version_id"] is None
    assert (
        len(client.get(f"/api/v1/models/{model['id']}/versions").json()["items"]) == versions_before
    )

    # Interrupted: the worker took the job and disappeared (its lease ran out).
    job = _repair(client, model["id"], broken["id"], {"code": "remove_duplicate_faces"}).json()
    claimed = runner.claim_next()
    assert str(claimed) == job["id"]
    with runner.database.session() as db:
        db.execute(
            update(GenerationJob)
            .where(GenerationJob.id == claimed)
            .values(lease_expires_at=utcnow() - timedelta(minutes=5))
        )
        db.commit()
    runner._active.pop(claimed, None)  # the old worker is gone
    assert runner.recover() == 1
    runner.run_until_idle()
    done = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert done["status"] == "succeeded", done
    items = client.get(f"/api/v1/models/{model['id']}/versions").json()["items"]
    assert len(items) == versions_before + 1  # exactly one new version
