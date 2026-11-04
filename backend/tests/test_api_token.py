"""The desktop app's per-launch token: required on every API request when configured."""

import logging
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai3d.cli import watch_parent
from ai3d.main import create_app
from tests.conftest import make_settings

TOKEN = "f3a1c0de" * 8


@pytest.fixture
def secured(tmp_path: Path, database_url: str) -> Iterator[TestClient]:
    app = create_app(make_settings(tmp_path, database_url, api_token=TOKEN))
    with TestClient(app) as client:
        yield client


def test_requests_without_the_token_are_refused(secured: TestClient) -> None:
    response = secured.get("/api/v1/projects")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"
    wrong = secured.get("/api/v1/projects", headers={"Authorization": "Bearer " + "0" * 64})
    assert wrong.status_code == 401
    assert secured.get("/api/v1/projects?access_token=nope").status_code == 401
    assert secured.get("/api/v1/openapi.json").status_code == 401


def test_the_token_is_accepted_as_header_or_query(secured: TestClient) -> None:
    ok = secured.get("/api/v1/projects", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    # Images, downloads and event streams are loaded by the browser itself.
    assert secured.get(f"/api/v1/projects?access_token={TOKEN}").status_code == 200


def test_health_stays_open_for_monitoring(secured: TestClient) -> None:
    assert secured.get("/api/v1/health").status_code == 200


def test_preflight_and_401s_carry_cors_headers(secured: TestClient) -> None:
    origin = "tauri://localhost"
    preflight = secured.options(
        "/api/v1/projects",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert preflight.status_code == 200
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
    refused = secured.get("/api/v1/projects", headers={"Origin": origin})
    assert refused.status_code == 401
    assert refused.headers["access-control-allow-origin"] == origin


def test_the_token_never_appears_in_request_logs(
    secured: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="ai3d.request"):
        secured.get(f"/api/v1/projects?access_token={TOKEN}")
    assert caplog.records
    assert all(TOKEN not in record.getMessage() for record in caplog.records)


def test_short_tokens_are_rejected_at_startup(tmp_path: Path, database_url: str) -> None:
    with pytest.raises(ValueError, match="at least 16"):
        make_settings(tmp_path, database_url, api_token="short")


def test_without_a_token_the_web_app_needs_none(client: TestClient) -> None:
    assert client.get("/api/v1/projects").status_code == 200


def test_backend_exits_when_the_desktop_app_is_gone() -> None:
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gone = threading.Event()
    watch_parent(parent.pid, interval=0.05, on_exit=gone.set)
    assert not gone.wait(0.3)  # still running: nothing happens
    parent.kill()
    parent.wait()
    assert gone.wait(5), "the backend did not notice that the app was gone"
