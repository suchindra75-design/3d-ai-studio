from fastapi.testclient import TestClient

from ai3d.core.config import Settings


def test_health_reports_ok_and_request_id(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["storage"] == "ok"
    assert len(response.headers["x-request-id"]) == 32


def test_incoming_request_id_is_propagated(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "client-abc.123"})
    assert response.headers["x-request-id"] == "client-abc.123"


def test_malformed_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert response.headers["x-request-id"] != "bad id\twith spaces"


def test_system_info_lists_modes_and_limits(client: TestClient, settings: Settings) -> None:
    body = client.get("/api/v1/system/info").json()
    assert body["database_backend"] == settings.database_backend
    assert body["storage_backend"] == settings.storage_backend
    assert {m["id"] for m in body["quality_modes"]} == {"fast", "high_quality", "professional"}
    assert {m["id"] for m in body["processing_modes"]} == {"auto", "local", "cloud"}
    assert body["limits"]["accepted_mime_types"] == ["image/jpeg", "image/png", "image/webp"]


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == response.headers["x-request-id"]


def test_openapi_schema_is_served(client: TestClient) -> None:
    schema = client.get("/api/v1/openapi.json").json()
    assert schema["info"]["title"] == "AI 3D Studio API"
    assert "/api/v1/projects/{project_id}/images" in schema["paths"]
    assert "ErrorResponse" in schema["components"]["schemas"]


def test_oversized_body_rejected_before_parsing(client: TestClient) -> None:
    response = client.post(
        "/api/v1/projects/00000000-0000-0000-0000-000000000000/images",
        headers={"content-length": str(10**12), "content-type": "multipart/form-data; b=x"},
        content=b"",
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_cors_allows_desktop_origin(client: TestClient) -> None:
    response = client.options(
        "/api/v1/projects",
        headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "POST"},
    )
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"


def test_cors_rejects_unknown_origin(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
