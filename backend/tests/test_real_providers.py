"""Checks against the real Tripo and Meshy services (docs/AI_PROVIDERS.md).

Never run by default and never in CI: they need your own API keys, and the
second level spends credits. The mock provider is not used here at all.

Level 1, free (reads the account balance; no task is created):

    AI3D_REAL_TRIPO_API_KEY=… AI3D_REAL_MESHY_API_KEY=… \\
        uv run pytest backend/tests/test_real_providers.py -m real_provider

Without any key, AI3D_REAL_PROVIDER_CHECKS=1 still checks that a wrong key is
refused in the services' current error format (free, nothing is sent but the key).

Level 2, PAID: one image-to-3D task and one re-texture task per provider named in
AI3D_ALLOW_PAID_PROVIDER_CALLS (e.g. "meshy" or "tripo,meshy"), with a real photo:

    AI3D_ALLOW_PAID_PROVIDER_CALLS=meshy AI3D_REAL_MESHY_API_KEY=… \\
    AI3D_REAL_PROVIDER_PHOTO=/path/to/photo.jpg \\
        uv run pytest backend/tests/test_real_providers.py -m real_provider -s

The paid tests print the credits each task consumed, as reported by the service.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai3d.main import create_app
from ai3d.processing.mesh import validate_mesh_file
from ai3d.providers.base import ProviderError, ProviderErrorCode
from ai3d.providers.meshy import MeshyProvider
from ai3d.providers.tripo import TripoProvider
from ai3d.workers import JobRunner
from tests.conftest import make_settings

pytestmark = pytest.mark.real_provider

PROVIDERS = {"tripo": TripoProvider, "meshy": MeshyProvider}


def _key(name: str) -> str | None:
    return os.environ.get(f"AI3D_REAL_{name.upper()}_API_KEY") or None


def _paid(name: str) -> bool:
    allowed = os.environ.get("AI3D_ALLOW_PAID_PROVIDER_CALLS", "")
    return name in {p.strip().lower() for p in allowed.split(",")}


def _needs_key(name: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(
        _key(name) is None, reason=f"set AI3D_REAL_{name.upper()}_API_KEY to check {name}"
    )


# -- level 1: free --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [pytest.param(n, marks=_needs_key(n)) for n in PROVIDERS],
)
def test_the_real_key_is_accepted_and_the_balance_read(name: str) -> None:
    provider = PROVIDERS[name](api_key=_key(name))
    try:
        check = provider.check_credentials()
    finally:
        provider.close()
    assert check.ok, check.message
    assert check.balance is not None, check.message  # both services document a balance
    print(f"\n{name}: {check.message}")


@pytest.mark.parametrize("name", list(PROVIDERS))
@pytest.mark.skipif(
    os.environ.get("AI3D_REAL_PROVIDER_CHECKS") != "1" and not any(_key(n) for n in PROVIDERS),
    reason="set AI3D_REAL_PROVIDER_CHECKS=1 (or a real key) to call the real services",
)
def test_a_wrong_key_is_refused_as_an_authentication_error(name: str) -> None:
    """The real error format still maps to "authentication failed" (no task is made)."""
    provider = PROVIDERS[name](api_key="ai3d-deliberately-invalid-key")
    try:
        with pytest.raises(ProviderError) as caught:
            provider.check_credentials()
    finally:
        provider.close()
    assert caught.value.code == ProviderErrorCode.AUTH_FAILED


# -- level 2: paid ----------------------------------------------------------------------


def _photo() -> Path:
    path = os.environ.get("AI3D_REAL_PROVIDER_PHOTO")
    if not path or not Path(path).is_file():
        pytest.skip("set AI3D_REAL_PROVIDER_PHOTO to a real photo of one object")
    return Path(path)


@pytest.fixture
def real_app(name: str, tmp_path: Path, database_url: str) -> Iterator[tuple[TestClient, Any]]:
    settings = make_settings(
        tmp_path,
        database_url,
        **{f"{name}_api_key": _key(name)},
        provider_poll_interval=5,  # real services: do not poll in a tight loop
    )
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.runner


def _run(runner: JobRunner, client: TestClient, job_id: str) -> dict[str, Any]:
    while runner.run_until_idle():
        pass
    job: dict[str, Any] = client.get(f"/api/v1/jobs/{job_id}").json()
    return job


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            n,
            marks=[
                _needs_key(n),
                pytest.mark.skipif(
                    not _paid(n), reason=f"PAID: add {n} to AI3D_ALLOW_PAID_PROVIDER_CALLS"
                ),
            ],
        )
        for n in PROVIDERS
    ],
)
def test_paid_generation_then_retexture_through_the_app(
    name: str, real_app: tuple[TestClient, JobRunner]
) -> None:
    """One real image-to-3D task, then one re-texture of its result, each saved as a
    validated version; the credits the service reports are printed."""
    client, runner = real_app
    photo = _photo()
    project = client.post(
        "/api/v1/projects",
        json={"name": f"Real {name}", "input_mode": "single_image", "allow_cloud_upload": True},
    ).json()
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/images",
        files={"files": (photo.name, photo.read_bytes(), "image/jpeg")},
    ).json()
    image_id = uploaded["results"][0]["image"]["id"]
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={
            "inputs": [{"image_id": image_id}],
            "provider": name,
            "quality_mode": "fast",
            "cloud_upload_consent": True,
        },
    ).json()
    done = _run(runner, client, job["id"])
    assert done["status"] == "succeeded", done.get("error")
    print(f"\n{name} generation cost: {done.get('cost')}")
    model = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"][0]
    version = model["current_version"]
    assert version["surface_provenance"] == "inferred"

    retexture = client.post(
        f"/api/v1/models/{model['id']}/retexture",
        json={
            "provider": name,
            "source_version_id": version["id"],
            "prompt": "weathered bronze with green patina",
            "cloud_upload_consent": True,
        },
    )
    assert retexture.status_code == 201, retexture.text
    finished = _run(runner, client, retexture.json()["id"])
    assert finished["status"] == "succeeded", finished.get("error")
    print(f"{name} re-texture cost: {finished.get('cost')}")
    textured = client.get(f"/api/v1/models/{model['id']}").json()["current_version"]
    assert textured["id"] != version["id"]
    assert textured["source"] == "enhanced"
    data = client.get(textured["file_url"]).content
    path = Path(os.environ.get("TMPDIR", "/tmp")) / f"real-{name}-retextured.glb"
    path.write_bytes(data)
    report = validate_mesh_file(path)
    assert report.usable
    assert report.metrics["texture_count"] >= 1
