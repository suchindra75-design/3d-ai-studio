from fastapi.testclient import TestClient


def test_local_device_detection_is_persisted(client: TestClient) -> None:
    first = client.get("/api/v1/devices/local")
    assert first.status_code == 200
    body = first.json()
    assert body["cpu_cores_logical"] >= 1
    assert body["ram_total_bytes"] > 0
    assert {
        "local_ai_generation",
        "photogrammetry_sparse",
        "photogrammetry_dense",
        "disk_space",
    } <= set(body["capabilities"])
    for capability in body["capabilities"].values():
        assert isinstance(capability["supported"], bool)
        assert capability["reason"]

    second = client.get("/api/v1/devices/local", params={"refresh": "true"}).json()
    assert second["id"] == body["id"]  # same machine -> same row
    assert second["last_seen_at"] >= body["last_seen_at"]
