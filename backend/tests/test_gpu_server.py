"""The app with your own GPU server: settings, routing, and whole jobs over real HTTP.

A real GPU server (``gpu-server/``) runs on a local port with its stand-in backend
(an ellipsoid instead of TRELLIS, no GPU needed). Everything else is real: the
upload with consent, the queue, polling, downloads, deleting the job on the server,
and refining on this computer, including turning the model to face the photo.
"""

import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
import uvicorn
from fastapi.testclient import TestClient

from ai3d.providers.gpu_server import check_server_url
from ai3d.workers import JobRunner
from ai3d_gpu.app import create_app as create_gpu_app
from ai3d_gpu.backends.stand_in import StandInBackend
from ai3d_gpu.config import ServerConfig

KEY = "gpu-server"
TOKEN = "s" * 40


class _TurnedStandIn(StandInBackend):
    """Like TRELLIS, makes the model in its own pose: turned 90° from the photo."""

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        made = super().generate(*args, **kwargs)
        a = np.radians(90.0)
        turn = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
        # A long nose on the front, so the outline shows which way the model faces.
        vertices = made.vertices.copy()
        front = vertices[:, 2] > 0.6 * vertices[:, 2].max()
        vertices[front, 2] *= 1.8
        made.vertices = vertices @ turn.T
        return made


@pytest.fixture
def gpu_server(tmp_path: Path) -> Iterator[tuple[str, ServerConfig]]:
    config = ServerConfig(token=TOKEN, backend="stand-in", work_dir=tmp_path / "gpu-work")
    app = create_gpu_app(config, _TurnedStandIn())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", config
    server.should_exit = True
    thread.join(timeout=5)


def _provider(client: TestClient) -> dict[str, Any]:
    items = client.get("/api/v1/providers").json()["items"]
    (found,) = [p for p in items if p["key"] == KEY]
    return dict(found)


def _configure(client: TestClient, url: str, token: str = TOKEN) -> None:
    assert (
        client.put(f"/api/v1/providers/{KEY}/base-url", json={"base_url": url}).status_code == 200
    )
    assert (
        client.put(f"/api/v1/providers/{KEY}/api-key", json={"api_key": token}).status_code == 200
    )


@pytest.mark.parametrize(
    ("url", "refused_with"),
    [
        ("https://gpu.example.com:8800", None),
        ("https://gpu.example.com/", None),
        ("http://127.0.0.1:8800", None),  # an SSH tunnel to the server
        ("http://localhost:8800", None),
        ("http://gpu.example.com:8800", "Use https://"),  # the token would travel in clear
        ("ftp://gpu.example.com", "Enter the server's address"),
        ("https://user:pass@gpu.example.com", "Enter only the server's address"),
        ("gpu.example.com", "Enter the server's address"),
    ],
)
def test_the_server_address_must_keep_the_token_safe(url: str, refused_with: str | None) -> None:
    if refused_with is None:
        assert check_server_url(url) == url.rstrip("/")
    else:
        with pytest.raises(ValueError, match=re.escape(refused_with)):
            check_server_url(url)


def test_the_provider_needs_both_an_address_and_a_token(client: TestClient) -> None:
    provider = _provider(client)
    assert provider["requires_base_url"] is True
    assert provider["requires_api_key"] is True
    assert provider["configured"] is False
    assert provider["base_url_env_var"] == "AI3D_GPU_SERVER_URL"
    assert provider["api_key_env_var"] == "AI3D_GPU_SERVER_TOKEN"

    refused = client.put(f"/api/v1/providers/{KEY}/base-url", json={"base_url": "http://x.org"})
    assert refused.status_code == 422
    assert "https://" in refused.json()["error"]["message"]

    client.put(f"/api/v1/providers/{KEY}/base-url", json={"base_url": "https://gpu.example.com"})
    provider = _provider(client)
    assert provider["base_url"] == "https://gpu.example.com"
    assert provider["configured"] is False  # no token yet
    client.put(f"/api/v1/providers/{KEY}/api-key", json={"api_key": TOKEN})
    provider = _provider(client)
    assert provider["configured"] is True
    assert provider["api_key_hint"] != TOKEN  # the token is never returned
    client.delete(f"/api/v1/providers/{KEY}/base-url")
    assert _provider(client)["configured"] is False


def test_check_connects_to_the_server(
    client: TestClient, gpu_server: tuple[str, ServerConfig]
) -> None:
    url, _ = gpu_server
    _configure(client, url)
    check = client.post(f"/api/v1/providers/{KEY}/check").json()
    assert check["ok"] is True
    assert "stand-in" in check["message"]

    _configure(client, url, token="w" * 40)
    check = client.post(f"/api/v1/providers/{KEY}/check").json()
    assert check["ok"] is False
    assert "refused the access token" in check["message"]


def _generate(client: TestClient, project_id: str, inputs: list[dict[str, Any]]) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/projects/{project_id}/generations",
        json={
            "inputs": inputs,
            "provider": KEY,
            "processing_mode": "cloud",
            "cloud_upload_consent": True,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_a_photo_becomes_a_refined_model_on_your_gpu_server(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    gpu_server: tuple[str, ServerConfig],
) -> None:
    url, config = gpu_server
    _configure(client, url)
    project, ids = make_image_project(1, allow_cloud_upload=True, default_quality_mode="fast")
    plan = client.post(
        f"/api/v1/projects/{project['id']}/generations/plan",
        json={"inputs": [{"image_id": ids[0]}], "provider": KEY, "processing_mode": "cloud"},
    ).json()
    assert plan["ok"] is True, plan
    assert plan["requires_consent"] is True  # photos leave this computer (to your server)
    assert any("TRELLIS in 12 + 12 steps" in line for line in plan["provider_summary"])

    job = _generate(client, project["id"], [{"image_id": ids[0]}])
    runner.run_until_idle()
    job = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == "succeeded", job
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    (refined,) = [e for e in events if (e["message"] or "").startswith("Refined in")]
    # Made in its own pose, then turned to face the photo (the exact turn is tested
    # in test_refine.py with a photo that shows the shape; these test photos do not).
    assert "Turned" in refined["message"]
    assert refined["data"]["refine"]["orientation"]["outline_overlap"] > 0

    version = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"][0][
        "current_version"
    ]
    assert version["provider_key"] == KEY
    data = client.get(version["file_url"]).content
    mesh = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="glb", force="mesh")
    assert isinstance(mesh.visual, trimesh.visual.TextureVisuals)  # baked here
    assert mesh.visual.material.baseColorTexture.size == (1024, 1024)  # the fast profile
    # The server deleted the job once the app had its files.
    assert not any((config.work_dir).iterdir())


def test_four_views_go_to_the_server_together(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    gpu_server: tuple[str, ServerConfig],
) -> None:
    url, _ = gpu_server
    _configure(client, url)
    project, ids = make_image_project(
        3, input_mode="multi_view", allow_cloud_upload=True, default_quality_mode="fast"
    )
    inputs = [
        {"image_id": ids[0], "role": "front"},
        {"image_id": ids[1], "role": "left"},
        {"image_id": ids[2], "role": "back"},
    ]
    job = _generate(client, project["id"], inputs)
    runner.run_until_idle()
    job = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == "succeeded", job
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    (refined,) = [e for e in events if (e["message"] or "").startswith("Refined in")]
    details = refined["data"]["refine"]
    assert {v["role"] for v in details["views"]} == {"left", "back"}
    assert details["depth"] is None  # TRELLIS shaped every side; photos only colour them


def test_a_wrong_token_fails_the_job_without_retrying(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    gpu_server: tuple[str, ServerConfig],
) -> None:
    url, _ = gpu_server
    _configure(client, url, token="w" * 40)
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _generate(client, project["id"], [{"image_id": ids[0]}])
    runner.run_until_idle()
    job = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == "failed"
    assert job["error"]["code"] == "auth_failed"
    assert "access token" in job["error"]["message"]
