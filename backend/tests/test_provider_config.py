"""Provider listing and API key storage."""

import logging
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from ai3d.core.secrets import KEY_FILE_NAME, SecretBox, SecretDecryptionError, secret_hint
from ai3d.database.session import Database
from ai3d.main import create_app
from ai3d.models import ProviderConfiguration
from ai3d.providers import ProviderRegistry
from tests.conftest import make_settings

KEY = "tsk_live_abcdefghijklmnop1234"


def _provider(client: TestClient, key: str) -> dict[str, object]:
    items = client.get("/api/v1/providers").json()["items"]
    return next(p for p in items if p["key"] == key)


def test_list_providers(client: TestClient) -> None:
    items = {p["key"]: p for p in client.get("/api/v1/providers").json()["items"]}
    assert list(items) == [
        "gpu-server",
        "tripo",
        "meshy",
        "local-photogrammetry",
        "local-triposr",
        "mock",
    ]
    gpu = items["gpu-server"]
    assert gpu["configured"] is False  # needs an address and a token
    assert gpu["requires_base_url"] is True
    assert gpu["uploads_images"] is True  # photos leave this computer (to your server)
    tripo = items["tripo"]
    assert tripo["configured"] is False
    assert tripo["credential_source"] == "none"
    assert tripo["api_key_env_var"] == "AI3D_TRIPO_API_KEY"
    assert tripo["uploads_images"] is True
    assert tripo["capabilities"]["cancel"] == "unsupported"
    assert items["meshy"]["capabilities"]["cancel"] == "pending_only"
    assert items["mock"]["kind"] == "mock"
    assert items["mock"]["configured"] is True


def test_mock_provider_only_exists_in_test_environment(tmp_path: Path) -> None:
    for environment in ("development", "production"):
        settings = make_settings(tmp_path, None, environment=environment)
        assert ProviderRegistry(settings).get("mock") is None
    assert ProviderRegistry(make_settings(tmp_path, None)).get("mock") is not None


def test_store_key_is_encrypted_and_never_returned(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    response = client.put("/api/v1/providers/tripo/api-key", json={"api_key": f"  {KEY} "})
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["credential_source"] == "stored"
    assert body["api_key_hint"] == "…1234"
    assert KEY not in response.text
    assert KEY not in client.get("/api/v1/providers").text
    assert KEY not in caplog.text

    database: Database = client.app.state.db  # type: ignore[attr-defined]
    with database.session() as db:
        config = db.scalar(select(ProviderConfiguration))
        assert config is not None
        assert config.api_key_encrypted is not None
        assert KEY not in config.api_key_encrypted
        box: SecretBox = client.app.state.providers.secrets  # type: ignore[attr-defined]
        assert box.decrypt(config.api_key_encrypted) == KEY

    removed = client.delete("/api/v1/providers/tripo/api-key").json()
    assert removed["configured"] is False
    assert removed["api_key_hint"] is None


@pytest.mark.parametrize("value", ["has space inside", "x" * 513, "   "])
def test_invalid_keys_are_rejected(client: TestClient, value: str) -> None:
    response = client.put("/api/v1/providers/meshy/api-key", json={"api_key": value})
    assert response.status_code == 422


def test_unknown_provider(client: TestClient) -> None:
    assert client.put("/api/v1/providers/nope/api-key", json={"api_key": KEY}).status_code == 404
    assert client.post("/api/v1/providers/mock/api-key").status_code == 405


def test_environment_key_takes_precedence(tmp_path: Path, database_url: str) -> None:
    settings = make_settings(tmp_path, database_url, meshy_api_key="msy_from_environment_9876")
    with TestClient(create_app(settings)) as client:
        client.put("/api/v1/providers/meshy/api-key", json={"api_key": KEY})
        meshy = _provider(client, "meshy")
    assert meshy["credential_source"] == "environment"
    assert meshy["api_key_hint"] == "…9876"


def test_changed_secret_key_is_reported(tmp_path: Path, database_url: str) -> None:
    first = make_settings(tmp_path, database_url, secret_key=SecretStr("first-secret-passphrase"))
    with TestClient(create_app(first)) as client:
        client.put("/api/v1/providers/tripo/api-key", json={"api_key": KEY})
    second = make_settings(tmp_path, database_url, secret_key=SecretStr("another-passphrase"))
    with TestClient(create_app(second)) as client:
        tripo = _provider(client, "tripo")
        assert tripo["configured"] is False
        assert "AI3D_SECRET_KEY" in str(tripo["credential_error"])
        # A generation cannot silently use a broken credential.
        plan_body = {"inputs": [{"image_id": "00000000-0000-0000-0000-000000000000"}]}
        project = client.post(
            "/api/v1/projects", json={"name": "p", "input_mode": "single_image"}
        ).json()
        assert (
            client.post(
                f"/api/v1/projects/{project['id']}/generations/plan", json=plan_body
            ).status_code
            == 404
        )


def test_generated_key_file_is_private(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, None)
    box = SecretBox.from_settings(settings)
    key_file = settings.data_dir / KEY_FILE_NAME
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    token = box.encrypt("secret")
    # The same file yields the same key on the next start.
    assert SecretBox.from_settings(settings).decrypt(token) == "secret"
    other = SecretBox.from_settings(make_settings(tmp_path / "other", None))
    with pytest.raises(SecretDecryptionError):
        other.decrypt(token)


def test_secret_hint_never_reveals_short_secrets() -> None:
    assert secret_hint("short") == "…"
    assert secret_hint("abcdefghijklmnop") == "…mnop"


def test_secret_key_rules(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 16"):
        SecretBox.from_settings(make_settings(tmp_path, None, secret_key=SecretStr("short")))
    # An empty value (e.g. from docker compose) falls back to the private key file.
    SecretBox.from_settings(make_settings(tmp_path, None, secret_key=SecretStr("")))
    assert (tmp_path / "data" / KEY_FILE_NAME).exists()
