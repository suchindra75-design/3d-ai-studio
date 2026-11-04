"""Restarting the application: the real server process, killed and started again.

Covers "restart the application" and "recover project and job history" end to
end: a project with a finished generation and a generation that is running
when the server is killed with SIGKILL (no clean shutdown). After a restart on
the same data folder the project, photo, finished model and job history are
all there, and the interrupted job resumes its existing provider task and
finishes. Uses the test-only mock provider (placeholder output).
"""

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.images import encode, textured

pytestmark = pytest.mark.slow

TOKEN = "restart-recovery-test-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Server:
    """``python -m ai3d.cli`` on a data folder, started and killed for real."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.process: subprocess.Popen[bytes] | None = None
        self.port = 0

    def start(self) -> httpx2.Client:
        self.port = _free_port()
        env = {
            **os.environ,
            "AI3D_DATA_DIR": str(self.data_dir),
            "AI3D_DATABASE_URL": "",
            "AI3D_ENVIRONMENT": "test",
            "AI3D_LOG_LEVEL": "WARNING",
            "AI3D_API_TOKEN": TOKEN,
            "AI3D_PROVIDER_POLL_INTERVAL": "0.3",
            "AI3D_JOB_LEASE_SECONDS": "5",
            # Mock tasks take ~25 polls (~8 s), long enough to kill the server mid-task.
            "AI3D_MOCK_POLLS_UNTIL_DONE": "25",
        }
        log = (self.data_dir.parent / "server.log").open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ai3d.cli", "--host", "127.0.0.1", "--port", str(self.port)],
            env=env,
            cwd=self.data_dir.parent,
            stdout=log,
            stderr=log,
        )
        client = httpx2.Client(
            base_url=f"http://127.0.0.1:{self.port}/api/v1",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=30,
        )
        deadline = time.monotonic() + 90
        while True:
            try:
                if client.get("/health").status_code == 200:
                    return client
            except httpx2.TransportError:
                pass
            assert self.process.poll() is None, "the server exited during start-up"
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.3)

    def kill(self) -> None:
        """SIGKILL: no shutdown handler runs, the job keeps its (now stale) lease."""
        assert self.process is not None
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=30)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=30)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    instance = Server(tmp_path / "data")
    yield instance
    instance.stop()


def _wait_for(client: httpx2.Client, job_id: str, predicate: Any, timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"/jobs/{job_id}").json()
        if predicate(job):
            return job
        assert time.monotonic() < deadline, f"job stuck at {job['status']} / {job.get('stage')}"
        time.sleep(0.3)


def _generate(client: httpx2.Client, project_id: str, image_id: str) -> str:
    response = client.post(
        f"/projects/{project_id}/generations",
        json={"inputs": [{"image_id": image_id}], "provider": "mock", "cloud_upload_consent": True},
    )
    assert response.status_code in (200, 201, 202), response.text
    return str(response.json()["id"])


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_a_killed_server_restarts_with_projects_history_and_resumed_jobs(server: Server) -> None:
    client = server.start()
    project = client.post(
        "/projects",
        json={"name": "Restart test", "input_mode": "single_image", "allow_cloud_upload": True},
    ).json()
    upload = client.post(
        f"/projects/{project['id']}/images",
        files={"files": ("mug.png", encode(textured(640, 480, seed=3), "PNG"), "image/png")},
    )
    image_id = upload.json()["results"][0]["image"]["id"]

    finished_id = _generate(client, project["id"], image_id)
    _wait_for(client, finished_id, lambda j: j["status"] == "succeeded")
    interrupted_id = _generate(client, project["id"], image_id)
    running = _wait_for(
        client,
        interrupted_id,
        lambda j: j["status"] == "running" and j.get("provider_task_id") and j["progress"] > 0,
    )
    task_id = running["provider_task_id"]
    server.kill()

    client = server.start()
    # Everything that was saved is still there.
    projects = client.get("/projects").json()["items"]
    assert [p["name"] for p in projects] == ["Restart test"]
    images = client.get(f"/projects/{project['id']}/images").json()["items"]
    assert [i["original_filename"] for i in images] == ["mug.png"]
    assert client.get(images[0]["thumbnail_url"].removeprefix("/api/v1")).status_code == 200
    assert client.get(f"/jobs/{finished_id}").json()["status"] == "succeeded"

    # The interrupted job resumes the same provider task (nothing is sent again).
    resumed = _wait_for(client, interrupted_id, lambda j: j["status"] == "succeeded", timeout=90)
    assert resumed["provider_task_id"] == task_id
    events = client.get(f"/jobs/{interrupted_id}/events").json()["items"]
    messages = [e["message"] for e in events]
    assert any("Resuming after an interruption" in (m or "") for m in messages), messages
    submits = [m for m in messages if m and "Sending" in m]
    assert len(submits) == 1, submits  # sent once, before the restart

    # Both results are models with downloadable, valid GLB files.
    models = client.get(f"/projects/{project['id']}/models").json()["items"]
    assert len(models) == 2
    for model in models:
        version = model["current_version"]
        glb = client.get(version["file_url"].removeprefix("/api/v1"))
        assert glb.status_code == 200
        assert glb.content[:4] == b"glTF"
