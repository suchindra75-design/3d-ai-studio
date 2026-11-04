"""Importing existing 3D files, and backing up and restoring projects."""

import io
import json
import zipfile
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.processing import importing
from ai3d.workers import JobRunner
from tests.test_model_versions import _generated_model, _save


def _import(client: TestClient, project_id: str, name: str, data: bytes, **form: Any) -> Any:
    return client.post(
        f"/api/v1/projects/{project_id}/models/import",
        files={"file": (name, data, "application/octet-stream")},
        data={k: str(v).lower() if isinstance(v, bool) else v for k, v in form.items()},
    )


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def project(make_project: Any) -> dict[str, Any]:
    return make_project(input_mode="single_image")


def test_an_stl_in_millimetres_with_z_up_becomes_a_metre_y_up_model(
    client: TestClient, project: dict[str, Any]
) -> None:
    # 20 wide (x), 30 deep (y), 50 tall (z), in millimetres, as slicers write it.
    box = trimesh.creation.box(extents=(20, 30, 50))
    response = _import(client, project["id"], "Bracket.stl", box.export(file_type="stl"))
    assert response.status_code == 201, response.text
    model = response.json()
    assert model["name"] == "Bracket"
    version = model["current_version"]
    assert version["source"] == "imported"
    assert version["surface_provenance"] == "unknown"
    assert "origin and accuracy are unknown" in version["notes"]
    assert "millimetres; Z up" in version["notes"]
    size = version["quality_report"]["metrics"]["bbox_size"]
    assert size == pytest.approx([0.02, 0.05, 0.03])  # glTF: metres, Y up
    assert "scale" not in version["stats"]  # size not taken as real unless asked

    real = _import(
        client, project["id"], "Bracket.stl", box.export(file_type="stl"), real_scale=True
    ).json()
    assert real["current_version"]["stats"]["scale"] == {
        "known": True,
        "axis": "longest",
        "length_mm": pytest.approx(50.0),
        "source": "file units",
    }


def test_an_obj_with_its_material_and_texture_in_a_zip_keeps_the_texture(
    client: TestClient, project: dict[str, Any]
) -> None:
    box = trimesh.creation.box(extents=(1, 2, 1))
    box.unmerge_vertices()
    rng = np.random.default_rng(1)
    box.visual = trimesh.visual.TextureVisuals(
        uv=rng.random((len(box.vertices), 2)),
        image=Image.fromarray(rng.integers(0, 255, (32, 32, 3), dtype=np.uint8)),
    )
    exported = trimesh.exchange.obj.export_obj(
        box, include_texture=True, return_texture=True, mtl_name="box.mtl"
    )
    obj_text, assets = exported
    data = _zip({"box/box.obj": obj_text.encode(), **{f"box/{k}": v for k, v in assets.items()}})
    response = _import(client, project["id"], "box.zip", data, units="m", up_axis="y")
    assert response.status_code == 201, response.text
    version = response.json()["current_version"]
    metrics = version["quality_report"]["metrics"]
    assert metrics["texture_count"] == 1
    assert metrics["bbox_size"] == pytest.approx([1, 2, 1])
    assert "OBJ (in a ZIP)" in version["notes"]


def test_a_ply_keeps_its_vertex_colours(client: TestClient, project: dict[str, Any]) -> None:
    sphere = trimesh.creation.icosphere(subdivisions=2)
    colours = np.tile(np.array([[200, 40, 10, 255]], dtype=np.uint8), (len(sphere.vertices), 1))
    sphere.visual = trimesh.visual.ColorVisuals(sphere, vertex_colors=colours)
    response = _import(client, project["id"], "ball.ply", sphere.export(file_type="ply"))
    assert response.status_code == 201, response.text
    file = client.get(response.json()["current_version"]["file_url"]).content
    mesh = trimesh.load(io.BytesIO(file), file_type="glb", force="mesh")
    assert np.asarray(mesh.visual.vertex_colors)[:, :3].mean(axis=0) == pytest.approx(
        [200, 40, 10], abs=1
    )


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("notes.txt", b"hello", "Import a GLB, OBJ, STL, PLY or 3MF"),
        ("scene.gltf", b"{}", "needs its .bin and images"),
        ("broken.stl", b"solid nothing here", "no triangles"),
        ("evil.zip", _zip({"../evil.obj": b"v 0 0 0\n"}), "is not allowed"),
        ("two.zip", _zip({"a.stl": b"x", "b.obj": b"y"}), "more than one model"),
        ("empty.zip", _zip({"readme.txt": b"no model"}), "holds no 3D model"),
    ],
)
def test_unsupported_unsafe_or_broken_files_are_refused(
    client: TestClient, project: dict[str, Any], name: str, data: bytes, message: str
) -> None:
    response = _import(client, project["id"], name, data)
    assert response.status_code == 422, response.text
    assert message in response.json()["error"]["message"]
    assert client.get(f"/api/v1/projects/{project['id']}/models").json()["items"] == []


def test_an_archive_that_unpacks_too_large_is_refused(
    client: TestClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importing, "MAX_ARCHIVE_BYTES", 1000)
    data = _zip({"big.stl": b"x" * 5000})
    response = _import(client, project["id"], "big.zip", data)
    assert response.status_code == 422
    assert "more than 1 GB" in response.json()["error"]["message"]


def test_backup_and_restore_bring_back_photos_models_and_version_history(
    client: TestClient, runner: JobRunner, make_image_project: Any
) -> None:
    model = _generated_model(client, runner, make_image_project)
    project_id = model["project_id"]
    moved = trimesh.creation.icosphere(subdivisions=2)
    moved.apply_translation([0.5, 0, 0])
    edited = _save(
        client,
        model["id"],
        model["current_version"]["id"],
        trimesh.Scene(moved).export(file_type="glb"),
        ['Moved "Sphere"'],
    ).json()
    client.patch(
        f"/api/v1/models/{model['id']}", json={"current_version_id": model["current_version"]["id"]}
    )
    stl = trimesh.creation.box(extents=(10, 10, 10)).export(file_type="stl")
    _import(client, project_id, "cube.stl", stl, real_scale=True)

    backup = client.get(f"/api/v1/projects/{project_id}/backup")
    assert backup.status_code == 200
    assert backup.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(backup.content)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        names = set(bundle.namelist())
    assert manifest["format"] == "ai3d-studio-project"
    assert len(manifest["images"]) == 1
    assert [len(m["versions"]) for m in manifest["models"]] == [2, 1]
    assert all(v["file"] in names for m in manifest["models"] for v in m["versions"])

    restored = client.post(
        "/api/v1/projects/restore",
        files={"file": ("backup.zip", backup.content, "application/zip")},
    )
    assert restored.status_code == 201, restored.text
    project = restored.json()
    assert project["id"] != project_id
    assert project["name"].endswith("(restored)")
    assert project["image_count"] == 1
    models = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"]
    assert len(models) == 2
    by_name = {m["name"]: m for m in models}
    first = next(m for m in models if m["name"] != "cube")
    versions = client.get(f"/api/v1/models/{first['id']}/versions").json()["items"]
    assert [v["version_number"] for v in versions] == [2, 1]
    assert versions[0]["source"] == "edited"
    assert versions[0]["parent_version_id"] == versions[1]["id"]
    assert versions[0]["notes"] == edited["notes"]
    assert first["current_version"]["version_number"] == 1  # current kept
    cube = by_name["cube"]["current_version"]
    assert cube["source"] == "imported"
    assert cube["stats"]["scale"]["known"] is True
    assert cube["quality_report"]["status"] in {"pass", "warn"}


def test_a_damaged_backup_restores_nothing(
    client: TestClient, runner: JobRunner, make_image_project: Any
) -> None:
    model = _generated_model(client, runner, make_image_project)
    backup = client.get(f"/api/v1/projects/{model['project_id']}/backup").content
    source = zipfile.ZipFile(io.BytesIO(backup))
    damaged = io.BytesIO()
    with zipfile.ZipFile(damaged, "w") as bundle:
        for info in source.infolist():
            data = source.read(info)
            bundle.writestr(info, b"not a model" if info.filename.endswith(".glb") else data)
    before = client.get("/api/v1/projects").json()["total"]
    response = client.post(
        "/api/v1/projects/restore",
        files={"file": ("backup.zip", damaged.getvalue(), "application/zip")},
    )
    assert response.status_code == 422
    assert "failed the file checks" in response.json()["error"]["message"]
    assert client.get("/api/v1/projects").json()["total"] == before

    for bad, message in [
        (b"not a zip", "could not be opened"),
        (_zip({"a.txt": b"x"}), "not an AI 3D Studio backup"),
    ]:
        response = client.post(
            "/api/v1/projects/restore", files={"file": ("x.zip", bad, "application/zip")}
        )
        assert response.status_code == 422
        assert message in response.json()["error"]["message"]
