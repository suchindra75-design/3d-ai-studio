from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

MakeProject = Callable[..., dict[str, Any]]


def test_create_project_defaults(client: TestClient) -> None:
    response = client.post(
        "/api/v1/projects", json={"name": "  Mug  ", "input_mode": "single_image"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Mug"
    assert body["status"] == "active"
    assert body["default_quality_mode"] == "fast"
    assert body["default_processing_mode"] == "auto"
    assert body["allow_cloud_upload"] is False
    assert body["image_count"] == 0


def test_create_project_validation(client: TestClient) -> None:
    for payload in (
        {"name": "", "input_mode": "single_image"},
        {"name": "   ", "input_mode": "single_image"},
        {"name": "x" * 201, "input_mode": "single_image"},
        {"name": "ok", "input_mode": "panorama"},
        {"name": "ok"},
    ):
        response = client.post("/api/v1/projects", json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"]["code"] == "validation_error"


def test_get_update_and_archive(client: TestClient, make_project: MakeProject) -> None:
    project = make_project()
    pid = project["id"]
    assert client.get(f"/api/v1/projects/{pid}").json()["name"] == "Test object"

    updated = client.patch(
        f"/api/v1/projects/{pid}",
        json={
            "name": "Renamed",
            "default_quality_mode": "professional",
            "allow_cloud_upload": True,
        },
    ).json()
    assert updated["name"] == "Renamed"
    assert updated["default_quality_mode"] == "professional"
    assert updated["allow_cloud_upload"] is True
    assert updated["updated_at"] >= project["updated_at"]

    archived = client.patch(f"/api/v1/projects/{pid}", json={"status": "archived"}).json()
    assert archived["status"] == "archived"
    assert archived["archived_at"] is not None
    restored = client.patch(f"/api/v1/projects/{pid}", json={"status": "active"}).json()
    assert restored["archived_at"] is None


def test_list_filter_search_and_paginate(client: TestClient, make_project: MakeProject) -> None:
    make_project(name="Red chair")
    make_project(name="Blue chair")
    vase = make_project(name="100% vase_v2")
    client.patch(f"/api/v1/projects/{vase['id']}", json={"status": "archived"})

    body = client.get("/api/v1/projects").json()
    assert body["total"] == 3
    # most recently updated first
    assert body["items"][0]["name"] == "100% vase_v2"

    assert client.get("/api/v1/projects", params={"q": "chair"}).json()["total"] == 2
    # LIKE wildcards in the search term are literal
    assert client.get("/api/v1/projects", params={"q": "%"}).json()["total"] == 1
    assert client.get("/api/v1/projects", params={"q": "e_v"}).json()["total"] == 1
    assert client.get("/api/v1/projects", params={"status": "archived"}).json()["total"] == 1

    page = client.get("/api/v1/projects", params={"limit": 2, "offset": 2}).json()
    assert page["total"] == 3
    assert len(page["items"]) == 1
    assert client.get("/api/v1/projects", params={"limit": 0}).status_code == 422


def test_missing_project_returns_404_shape(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    for method, kwargs in (("get", {}), ("patch", {"json": {"name": "x"}}), ("delete", {})):
        response = getattr(client, method)(f"/api/v1/projects/{missing}", **kwargs)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_invalid_uuid_is_validation_error(client: TestClient) -> None:
    response = client.get("/api/v1/projects/not-a-uuid")
    assert response.status_code == 422


def test_delete_project(client: TestClient, make_project: MakeProject) -> None:
    pid = make_project()["id"]
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 204
    assert client.get(f"/api/v1/projects/{pid}").status_code == 404
