"""Optional error reporting: off by default, and only scrubbed errors leave."""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai3d.core.error_reporting import scrub, setup_error_reporting
from ai3d.main import create_app
from tests.conftest import make_settings

# The `errors` extra (uv sync --extra errors).
sentry_sdk = pytest.importorskip("sentry_sdk")
from sentry_sdk.envelope import Envelope  # noqa: E402
from sentry_sdk.transport import Transport  # noqa: E402

DSN = "https://public@errors.example.invalid/1"


class Captured(Transport):
    """Keeps events in memory instead of sending them anywhere."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            if item.type == "event" and item.payload.json is not None:
                self.events.append(item.payload.json)


@pytest.fixture
def transport() -> Iterator[Captured]:
    captured = Captured()
    yield captured
    sentry_sdk.get_client().close()
    sentry_sdk.init()  # back to a no-op client for the rest of the suite


def test_off_without_a_dsn(tmp_path: Path, database_url: str) -> None:
    assert setup_error_reporting(make_settings(tmp_path, database_url)) is False
    assert not sentry_sdk.get_client().is_active()


def test_errors_are_reported_without_request_data_or_secrets(
    tmp_path: Path, database_url: str, transport: Captured
) -> None:
    settings = make_settings(tmp_path, database_url, sentry_dsn=DSN)
    app = create_app(settings)
    assert setup_error_reporting(settings, transport=transport) is True
    home = str(Path.home())

    @app.get("/api/v1/explode")
    def explode() -> None:
        raise RuntimeError(f"cannot read {home}/models/x.glb token=abc123 Bearer sk-live-999")

    with TestClient(app, raise_server_exceptions=False) as client:
        logging.getLogger("ai3d.test").warning("about to fail with api_key=sk-test-1")
        response = client.get(
            "/api/v1/explode?access_token=desktop-secret",
            headers={"Authorization": "Bearer desktop-secret", "Cookie": "session=1"},
        )
    assert response.status_code == 500
    sentry_sdk.flush()

    assert len(transport.events) == 1
    event = transport.events[0]
    text = repr(event)
    for secret in ("desktop-secret", "abc123", "sk-live-999", "sk-test-1", home, "session=1"):
        assert secret not in text, secret
    assert "request" not in event
    assert "user" not in event
    assert "server_name" not in event
    assert event["release"].startswith("ai3d-studio@")
    value = event["exception"]["values"][-1]["value"]
    assert value == "cannot read ~/models/x.glb token=[removed] Bearer [removed]"
    frames = event["exception"]["values"][-1]["stacktrace"]["frames"]
    assert all("vars" not in frame and "context_line" not in frame for frame in frames)


def test_scrub_handles_nested_values() -> None:
    event = {"a": ["?access_token=xyz&x=1", {"b": "/home/me/file"}], "n": 3}
    assert scrub(event, home="/home/me") == {
        "a": ["?access_token=[removed]&x=1", {"b": "~/file"}],
        "n": 3,
    }
