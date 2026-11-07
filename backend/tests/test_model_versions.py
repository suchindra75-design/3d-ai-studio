"""Edited versions saved from the 3D workspace, and switching between versions."""

import io
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from ai3d.core.config import Settings
from ai3d.workers import JobRunner
from tests.conftest import stored_keys

MakeImageProject = Callable[..., tuple[dict[str, Any], list[str]]]


def _version_folders(client: TestClient, model_dir: str) -> list[str]:
    """The vN folders stored under a model (data folder or S3)."""
    return sorted({key[len(model_dir) :].split("/")[0] for key in stored_keys(client, model_dir)})


def _generated_model(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> dict[str, Any]:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    response = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={
            "inputs": [{"image_id": ids[0]}],
            "provider": "mock",
            "cloud_upload_consent": True,
        },
    )
    assert response.status_code == 201, response.text
    assert runner.run_until_idle() == 1
    models = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"]
    assert len(models) == 1
    return dict(models[0])


def _scaled_glb(original: bytes, factor: float) -> bytes:
    """What the workspace exports: the same scene with a transformed node."""
    scene = trimesh.load_scene(io.BytesIO(original), file_type="glb")
    scene.apply_transform(np.diag([factor, factor, factor, 1.0]))
    data = scene.export(file_type="glb")
    assert isinstance(data, bytes)
    return data


def _save(
    client: TestClient,
    model_id: str,
    parent_id: str,
    data: bytes,
    changes: list[str] | None = None,
) -> Any:
    return client.post(
        f"/api/v1/models/{model_id}/versions",
        files={"file": ("model.glb", data, "model/gltf-binary")},
        data={"parent_version_id": parent_id, "changes": changes or []},
    )


@pytest.fixture
def model(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> dict[str, Any]:
    return _generated_model(client, runner, make_image_project)


def test_saving_an_edit_creates_a_new_version_and_keeps_the_original(
    client: TestClient, model: dict[str, Any]
) -> None:
    v1 = model["current_version"]
    original = client.get(v1["file_url"]).content

    response = _save(
        client,
        model["id"],
        v1["id"],
        _scaled_glb(original, 2.0),
        ["Scaled Icosphere to 2 × 2 × 2", "  Changed   colour  "],
    )
    assert response.status_code == 201, response.text
    v2 = response.json()
    assert v2["version_number"] == 2
    assert v2["parent_version_id"] == v1["id"]
    assert v2["source"] == "edited"
    # Editing does not make AI geometry measured; provenance is inherited.
    assert v2["surface_provenance"] == "inferred"
    assert v2["provider_key"] == "mock"
    assert v2["notes"].startswith(
        "Edited in the workspace from version 1: Scaled Icosphere to 2 × 2 × 2; Changed colour."
    )
    assert "not measurements" in v2["notes"]
    assert v2["quality_report"]["status"] == "pass"
    assert v2["has_preview"] is False  # the provider render shows the old version
    assert v2["stats"]["bbox_size"] == pytest.approx(
        [2 * s for s in v1["stats"]["bbox_size"]], rel=1e-4
    )

    current = client.get(f"/api/v1/models/{model['id']}").json()
    assert current["current_version"]["id"] == v2["id"]
    assert current["version_count"] == 2

    history = client.get(f"/api/v1/models/{model['id']}/versions").json()
    assert [v["version_number"] for v in history["items"]] == [2, 1]
    assert history["current_version_id"] == v2["id"]

    # The original file is untouched and both versions are downloadable.
    assert client.get(v1["file_url"]).content == original
    download = client.get(v2["file_url"])
    assert download.status_code == 200
    assert "-v2.glb" in download.headers["content-disposition"]
    reopened = trimesh.load_scene(io.BytesIO(download.content), file_type="glb")
    assert sum(len(g.faces) for g in reopened.geometry.values()) == v1["stats"]["triangle_count"]


def test_versions_chain_from_any_parent(client: TestClient, model: dict[str, Any]) -> None:
    v1 = model["current_version"]
    original = client.get(v1["file_url"]).content
    v2 = _save(client, model["id"], v1["id"], _scaled_glb(original, 2.0)).json()
    v3 = _save(client, model["id"], v1["id"], _scaled_glb(original, 0.5)).json()
    assert v2["version_number"] == 2
    assert v3["version_number"] == 3
    assert v3["parent_version_id"] == v1["id"]
    assert v3["notes"].startswith("Edited in the workspace from version 1: no changes recorded.")


def test_invalid_file_is_rejected_and_nothing_is_stored(
    client: TestClient, model: dict[str, Any], settings: Settings
) -> None:
    v1 = model["current_version"]
    response = _save(client, model["id"], v1["id"], b"glTF" + b"\x00" * 64)
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_input"
    assert body["details"]["issues"][0]["code"] == "unreadable"

    assert client.get(f"/api/v1/models/{model['id']}").json()["version_count"] == 1
    model_dir = f"projects/{model['project_id']}/models/{model['id']}/"
    assert _version_folders(client, model_dir) == ["v1"]
    assert list((settings.data_dir / "tmp").glob("model-*")) == []


def test_empty_file_is_rejected(client: TestClient, model: dict[str, Any]) -> None:
    response = _save(client, model["id"], model["current_version"]["id"], b"")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_file"


def test_oversized_file_is_rejected(
    client: TestClient, model: dict[str, Any], settings: Settings
) -> None:
    settings.max_model_bytes = 1024
    v1 = model["current_version"]
    original = client.get(v1["file_url"]).content
    assert len(original) > 1024
    response = _save(client, model["id"], v1["id"], original)
    assert response.status_code == 413
    assert response.json()["error"]["details"] == {"max_model_bytes": 1024}


def test_parent_must_belong_to_the_model(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    first = _generated_model(client, runner, make_image_project)
    second = _generated_model(client, runner, make_image_project)
    original = client.get(first["current_version"]["file_url"]).content
    response = _save(client, first["id"], second["current_version"]["id"], original)
    assert response.status_code == 422
    assert "different model" in response.json()["error"]["message"]

    unknown = _save(client, first["id"], "00000000-0000-4000-8000-000000000000", original)
    assert unknown.status_code == 404


def test_switch_current_version_and_rename(client: TestClient, model: dict[str, Any]) -> None:
    v1 = model["current_version"]
    original = client.get(v1["file_url"]).content
    v2 = _save(client, model["id"], v1["id"], _scaled_glb(original, 2.0)).json()

    response = client.patch(f"/api/v1/models/{model['id']}", json={"current_version_id": v1["id"]})
    assert response.status_code == 200
    assert response.json()["current_version"]["id"] == v1["id"]
    assert response.json()["version_count"] == 2  # switching deletes nothing

    renamed = client.patch(f"/api/v1/models/{model['id']}", json={"name": "  Mug, cleaned  "})
    assert renamed.json()["name"] == "Mug, cleaned"
    assert renamed.json()["current_version"]["id"] == v1["id"]

    assert client.patch(f"/api/v1/models/{model['id']}", json={"name": ""}).status_code == 422
    assert client.get(v2["file_url"]).status_code == 200


def test_cannot_switch_to_another_models_version(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    first = _generated_model(client, runner, make_image_project)
    second = _generated_model(client, runner, make_image_project)
    response = client.patch(
        f"/api/v1/models/{first['id']}",
        json={"current_version_id": second["current_version"]["id"]},
    )
    assert response.status_code == 422


def test_deleting_a_model_removes_every_version(
    client: TestClient, model: dict[str, Any], settings: Settings
) -> None:
    v1 = model["current_version"]
    original = client.get(v1["file_url"]).content
    v2 = _save(client, model["id"], v1["id"], _scaled_glb(original, 2.0)).json()
    model_dir = f"projects/{model['project_id']}/models/{model['id']}/"
    assert _version_folders(client, model_dir) == ["v1", "v2"]

    assert client.delete(f"/api/v1/models/{model['id']}").status_code == 204
    assert stored_keys(client, model_dir) == []
    assert client.get(v2["file_url"]).status_code == 404
    assert client.get(f"/api/v1/models/{model['id']}/versions").status_code == 404


def test_saving_a_model_too_large_for_free_memory_says_so(
    client: TestClient, model: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai3d.processing import mesh as mesh_module  # noqa: PLC0415

    class Memory:
        available = 64 * 1024 * 1024  # less than the check needs for any model

    monkeypatch.setattr(mesh_module.psutil, "virtual_memory", Memory)
    glb = trimesh.creation.box().export(file_type="glb")
    response = _save(client, model["id"], model["current_version"]["id"], glb)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "not_enough_memory"
    assert error["message"].startswith("Checking this 12-triangle model needs about")
    versions = client.get(f"/api/v1/models/{model['id']}/versions").json()["items"]
    assert len(versions) == 1  # nothing was saved


def test_an_interrupted_upload_saves_nothing_and_leaves_no_files(
    client: TestClient, model: dict[str, Any], settings: Settings
) -> None:
    """A connection dropped mid-upload (or a browser closed while saving) delivers a
    truncated file: no version is made, nothing is left in temporary storage, and the
    current version is untouched."""
    current = model["current_version"]
    original = client.get(current["file_url"]).content
    whole = trimesh.creation.icosphere(subdivisions=4).export(file_type="glb")
    for cut in (len(whole) // 2, 30, 0):
        response = _save(client, model["id"], current["id"], whole[:cut])
        assert response.status_code in {400, 413, 422}, response.text
    versions = client.get(f"/api/v1/models/{model['id']}/versions").json()["items"]
    assert [v["id"] for v in versions] == [current["id"]]
    assert client.get(current["file_url"]).content == original
    tmp = settings.data_dir / "tmp"
    assert not tmp.exists() or not any(tmp.iterdir())
