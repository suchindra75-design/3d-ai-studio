"""Enhance (new processed versions) and export (re-opened files) through the API."""

import importlib.util
import io
import zipfile
from collections.abc import Callable
from typing import Any

import pytest
import trimesh
from fastapi.testclient import TestClient

from ai3d.models.enums import ExportFormat
from ai3d.processing import exporters
from ai3d.workers import JobRunner
from ai3d_engine.blender import check_blender
from tests.test_model_versions import _generated_model, _save, _scaled_glb

MakeImageProject = Callable[..., tuple[dict[str, Any], list[str]]]
USD = importlib.util.find_spec("pxr") is not None
BLENDER = check_blender() is None


@pytest.fixture
def model(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> dict[str, Any]:
    return _generated_model(client, runner, make_image_project)


def _enhance(client: TestClient, model_id: str, **body: Any) -> Any:
    return client.post(f"/api/v1/models/{model_id}/enhance", json=body)


# -- enhance ------------------------------------------------------------------------------


def test_enhance_plan_lists_steps_and_tool_availability(
    client: TestClient, model: dict[str, Any]
) -> None:
    plan = client.post(
        f"/api/v1/models/{model['id']}/enhance/plan",
        json={"quality_mode": "professional", "scale": {"axis": "y", "length_mm": 80}},
    ).json()
    steps = {s["code"]: s for s in plan["steps"]}
    assert steps["normals"]["status"] == "will_run"
    assert "retopology" not in steps  # an option (remesh), not part of a profile
    assert steps["baking"]["status"] == ("will_run" if BLENDER else "unavailable")
    if not BLENDER:
        assert "Blender" in steps["baking"]["detail"]
    assert steps["scale"]["detail"].endswith("80 mm.")
    assert any("kept unchanged" in note for note in plan["notes"])
    fast = client.post(
        f"/api/v1/models/{model['id']}/enhance/plan", json={"quality_mode": "fast"}
    ).json()
    assert [s["code"] for s in fast["steps"]] == ["cleanup", "small_parts", "textures"]
    assert any("size stays unknown" in note for note in fast["notes"])


def test_enhance_with_scale_creates_a_new_version_and_keeps_the_source(
    client: TestClient, runner: JobRunner, model: dict[str, Any]
) -> None:
    v1 = model["current_version"]
    original = client.get(v1["file_url"]).content
    response = _enhance(
        client,
        model["id"],
        quality_mode="high_quality",
        scale={"axis": "longest", "length_mm": 250},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["kind"] == "enhance"
    assert job["source_model_version_id"] == v1["id"]
    assert job["provider"] is None
    assert runner.run_until_idle() == 1

    final = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert final["status"] == "succeeded", final["error"]
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    assert "Real-world scale: Scaled" in " ".join(e["message"] for e in events)

    current = client.get(f"/api/v1/models/{model['id']}").json()["current_version"]
    assert current["id"] == final["result_model_version_id"]
    assert current["version_number"] == 2
    assert current["source"] == "enhanced"
    assert current["parent_version_id"] == v1["id"]
    assert current["surface_provenance"] == v1["surface_provenance"]
    assert current["stats"]["scale"]["known"] is True
    assert max(current["stats"]["bbox_size"]) == pytest.approx(0.25, rel=1e-4)
    assert "Size set from your measurement" in current["notes"]
    report = current["quality_report"]
    assert report["report_version"] == 2
    assert {s["code"] for s in report["metrics"]["enhancement"]} >= {"cleanup", "scale"}

    # The source version's file is untouched.
    assert client.get(v1["file_url"]).content == original

    # Later edits keep the known scale.
    edited = _save(
        client, model["id"], current["id"], _scaled_glb(client.get(current["file_url"]).content, 1)
    ).json()
    assert edited["stats"]["scale"]["length_mm"] == 250


def test_enhance_without_changes_creates_no_version(
    client: TestClient, runner: JobRunner, model: dict[str, Any]
) -> None:
    job = _enhance(client, model["id"], quality_mode="fast").json()
    runner.run_until_idle()
    final = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert final["status"] == "succeeded"
    assert final["result_model_version_id"] is None
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    assert events[-1]["message"] == "Nothing needed changing, so no new version was created."
    assert client.get(f"/api/v1/models/{model['id']}").json()["version_count"] == 1


def test_enhance_validation_and_idempotency(
    client: TestClient,
    runner: JobRunner,
    make_image_project: MakeImageProject,
    model: dict[str, Any],
) -> None:
    other = _generated_model(client, runner, make_image_project)
    wrong = _enhance(
        client,
        model["id"],
        quality_mode="fast",
        source_version_id=other["current_version"]["id"],
    )
    assert wrong.status_code == 422
    bad_scale = _enhance(
        client, model["id"], quality_mode="fast", scale={"axis": "y", "length_mm": 0}
    )
    assert bad_scale.status_code == 422

    headers = {"Idempotency-Key": "enhance-once"}
    body = {"quality_mode": "professional"}
    first = client.post(f"/api/v1/models/{model['id']}/enhance", json=body, headers=headers)
    again = client.post(f"/api/v1/models/{model['id']}/enhance", json=body, headers=headers)
    assert first.json()["id"] == again.json()["id"]


# -- export -------------------------------------------------------------------------------


def test_formats_say_what_is_available(client: TestClient) -> None:
    items = {f["format"]: f for f in client.get("/api/v1/exports/formats").json()["items"]}
    assert items["glb"]["available"] is True
    assert items["stl"]["keeps_texture"] is False
    assert items["fbx"]["available"] is BLENDER
    if not BLENDER:
        assert "Blender" in items["fbx"]["unavailable_reason"]
    assert items["usdz"]["available"] is USD


AVAILABLE = [f.format.value for f in exporters.formats() if f.available]


@pytest.mark.parametrize("fmt", AVAILABLE)
def test_every_available_format_is_reopened_and_matches(
    client: TestClient, model: dict[str, Any], fmt: str
) -> None:
    version = model["current_version"]
    response = client.post(f"/api/v1/model-versions/{version['id']}/exports", json={"format": fmt})
    assert response.status_code == 201, response.text
    export = response.json()
    assert export["status"] == "ready", export["error_message"]
    validation = export["validation"]
    assert validation["triangles"] == version["stats"]["triangle_count"]
    millimetres = fmt in {"stl", "3mf"}
    x, y, z = version["stats"]["bbox_size"]
    expected = [v * 1000 for v in (x, z, y)] if millimetres else [x, y, z]  # Z up for printing
    assert validation["size"] == pytest.approx(expected, rel=1e-3)
    assert any("scale is unknown" in note for note in export["notes"])

    download = client.get(export["file_url"])
    assert download.status_code == 200
    data = download.content
    assert len(data) == export["size_bytes"]
    disposition = download.headers["content-disposition"]
    if fmt in {"gltf", "obj"}:
        assert disposition.endswith(f'-v1-{fmt}.zip"')
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert f"model.{fmt}" in names
    elif fmt == "fbx":
        assert data.startswith(b"Kaydara FBX Binary")
        assert validation["reopened_with"].endswith("FBX importer")
    elif fmt in {"glb", "stl", "3mf"}:
        reopened = trimesh.load(io.BytesIO(data), file_type=fmt, force="scene")
        assert sum(len(g.faces) for g in reopened.geometry.values()) == validation["triangles"]


def test_a_file_that_does_not_match_is_never_offered(
    client: TestClient, model: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = exporters.write

    def broken(scene: trimesh.Scene, fmt: ExportFormat, dest: Any, **kwargs: Any) -> None:
        real_write(scene, fmt, dest, **kwargs)
        dest.write_bytes(trimesh.creation.box().export(file_type="glb"))  # wrong geometry

    monkeypatch.setattr(exporters, "write", broken)
    version = model["current_version"]
    export = client.post(
        f"/api/v1/model-versions/{version['id']}/exports", json={"format": "glb"}
    ).json()
    assert export["status"] == "failed"
    assert "does not match the model" in export["error_message"]
    assert export["file_url"] is None
    assert client.get(f"/api/v1/exports/{export['id']}/file").status_code == 404


def test_unavailable_format_and_export_listing(
    client: TestClient, model: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # FBX is unavailable without Blender; simulate that so this runs either way.
    monkeypatch.setattr(exporters, "check_blender", lambda: "Needs Blender 4.2 or newer.")
    version_id = model["current_version"]["id"]
    fbx = client.post(f"/api/v1/model-versions/{version_id}/exports", json={"format": "fbx"})
    assert fbx.status_code == 422
    assert fbx.json()["error"]["code"] == "format_unavailable"

    stl = client.post(f"/api/v1/model-versions/{version_id}/exports", json={"format": "stl"})
    listing = client.get(f"/api/v1/model-versions/{version_id}/exports").json()["items"]
    assert [e["id"] for e in listing] == [stl.json()["id"]]
    assert client.delete(f"/api/v1/exports/{stl.json()['id']}").status_code == 204
    assert client.get(f"/api/v1/model-versions/{version_id}/exports").json()["items"] == []


def test_failed_enhance_can_be_retried(
    client: TestClient, runner: JobRunner, model: dict[str, Any]
) -> None:
    job = _enhance(client, model["id"], quality_mode="high_quality").json()
    assert client.post(f"/api/v1/jobs/{job['id']}/cancel").status_code == 200
    retried = client.post(f"/api/v1/jobs/{job['id']}/retry", json={})
    assert retried.status_code == 201, retried.text
    new = retried.json()
    assert new["kind"] == "enhance"
    assert new["parent_job_id"] == job["id"]
    assert new["source_model_version_id"] == job["source_model_version_id"]
    runner.run_until_idle()
    assert client.get(f"/api/v1/jobs/{new['id']}").json()["status"] == "succeeded"


def test_an_export_that_would_not_fit_in_memory_is_refused(
    client: TestClient, model: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai3d.processing import mesh as mesh_module  # noqa: PLC0415

    class Memory:
        available = 1024

    version_id = model["current_version"]["id"]
    with monkeypatch.context() as patch:
        patch.setattr(mesh_module.psutil, "virtual_memory", Memory)
        refused = client.post(
            f"/api/v1/model-versions/{version_id}/exports", json={"format": "stl"}
        )
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "not_enough_memory"
    assert error["message"].startswith("Writing ")
    assert "triangles as STL needs about" in error["message"]
    listed = client.get(f"/api/v1/model-versions/{version_id}/exports").json()["items"]
    assert listed == []  # nothing recorded
    ok = client.post(f"/api/v1/model-versions/{version_id}/exports", json={"format": "stl"})
    assert ok.json()["status"] == "ready"
