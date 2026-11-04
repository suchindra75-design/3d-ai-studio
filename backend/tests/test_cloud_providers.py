"""Tripo and Meshy adapters against in-process fakes of their documented APIs.

These prove the adapters build the documented requests and handle the
documented responses. They do not prove a real account works.
"""

import base64
import io
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.main import create_app
from ai3d.models.enums import JobKind, QualityMode
from ai3d.providers.base import GenerationRequest, ProviderError, ProviderErrorCode, ProviderInput
from ai3d.providers.meshy import MeshyProvider
from ai3d.providers.tripo import TripoProvider
from ai3d.workers import JobRunner
from tests import images
from tests.conftest import make_settings
from tests.fakes import CDN, FakeMeshy, FakeTripo

TRIPO_KEY = "tsk_test_key_123456"
MESHY_KEY = "msy_test_key_123456"


@pytest.fixture
def tripo() -> FakeTripo:
    return FakeTripo()


@pytest.fixture
def meshy() -> FakeMeshy:
    return FakeMeshy()


@pytest.fixture
def cloud_client(
    tmp_path: Path, database_url: str, tripo: FakeTripo, meshy: FakeMeshy
) -> Iterator[TestClient]:
    def route(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.meshy.ai" or "/meshy/" in str(request.url):
            return meshy.handle(request)
        return tripo.handle(request)

    settings = make_settings(
        tmp_path, database_url, tripo_api_key=TRIPO_KEY, meshy_api_key=MESHY_KEY
    )
    app = create_app(settings, provider_transport=httpx2.MockTransport(route))
    with TestClient(app) as client:
        yield client


def _project_with_photos(client: TestClient, count: int) -> tuple[str, list[str]]:
    project = client.post(
        "/api/v1/projects",
        json={"name": "Cloud", "input_mode": "single_image", "allow_cloud_upload": True},
    ).json()
    files = [
        ("files", (f"p{i}.jpg", images.encode(images.textured(seed=i), "JPEG"), "image/jpeg"))
        for i in range(count)
    ]
    result = client.post(f"/api/v1/projects/{project['id']}/images", files=files).json()
    return project["id"], [r["image"]["id"] for r in result["results"]]


def _run(client: TestClient, project_id: str, ids: list[str], **body: Any) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/projects/{project_id}/generations",
        json={"inputs": [{"image_id": i} for i in ids], "cloud_upload_consent": True, **body},
    )
    assert response.status_code == 201, response.text
    runner: JobRunner = client.app.state.runner  # type: ignore[attr-defined]
    runner.run_until_idle()
    return dict(client.get(f"/api/v1/jobs/{response.json()['id']}").json())


# -- Tripo ----------------------------------------------------------------------


def test_tripo_single_image_pipeline(cloud_client: TestClient, tripo: FakeTripo) -> None:
    project_id, ids = _project_with_photos(cloud_client, 1)
    job = _run(cloud_client, project_id, ids, provider="tripo", quality_mode="high_quality")
    assert job["status"] == "succeeded", job["error"]
    assert job["provider_task_id"] == "task-1"
    assert job["cost"] == {"credits": 40.0, "usd": 0.4, "source": "provider"}
    assert job["estimate"]["amount"] == 40

    upload = tripo.calls("POST", "/files")[0]
    assert upload.headers["authorization"] == f"Bearer {TRIPO_KEY}"
    assert b'name="file"' in upload.body
    assert b"Exif" not in upload.body  # the metadata-free copy was sent
    create = tripo.calls("POST", "/generation/image-to-model")[0].json()
    assert create == {
        "input": "file_0",
        "model": "v3.1-20260211",
        "texture": True,
        "pbr": True,
        "texture_quality": "detailed",
        "geometry_quality": "standard",
    }
    downloads = [r for r in tripo.requests if r.url.startswith(CDN)]
    assert len(downloads) == 2  # model + preview
    # Signed CDN links must never receive the API key.
    assert all("authorization" not in r.headers for r in downloads)

    models = cloud_client.get(f"/api/v1/projects/{project_id}/models").json()["items"]
    version = models[0]["current_version"]
    assert version["provider_display_name"] == "Tripo AI"
    assert "invented by the AI model" in version["notes"]
    assert version["preview_url"] is not None


def test_tripo_multiview_uses_view_keys(cloud_client: TestClient, tripo: FakeTripo) -> None:
    project_id, ids = _project_with_photos(cloud_client, 3)
    roles = ["left", "front", "back"]
    response = cloud_client.post(
        f"/api/v1/projects/{project_id}/generations",
        json={
            "inputs": [{"image_id": i, "role": r} for i, r in zip(ids, roles, strict=True)],
            "provider": "tripo",
            "cloud_upload_consent": True,
            "quality_mode": "fast",
        },
    )
    assert response.status_code == 201
    cloud_client.app.state.runner.run_until_idle()  # type: ignore[attr-defined]
    body = tripo.calls("POST", "/generation/multiview-to-model")[0].json()
    # Uploaded in input order (file_0 = left, file_1 = front, file_2 = back), sent front-first.
    assert body["inputs"] == [{"front": "file_1"}, {"left": "file_0"}, {"back": "file_2"}]
    assert body["pbr"] is False


def test_tripo_expired_download_link_is_refreshed(
    cloud_client: TestClient, tripo: FakeTripo
) -> None:
    tripo.cdn_failures = [403]
    project_id, ids = _project_with_photos(cloud_client, 1)
    job = _run(cloud_client, project_id, ids, provider="tripo")
    assert job["status"] == "succeeded", job["error"]
    model_downloads = [r.url for r in tripo.requests if r.url.startswith(CDN) and ".glb" in r.url]
    assert len(model_downloads) == 2
    assert model_downloads[0] != model_downloads[1]  # fresh link from a new status call


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx2.Response(401, json={"code": 1002, "message": "bad key"}), "auth_failed"),
        (
            httpx2.Response(403, json={"code": 2010, "message": "Insufficient credits"}),
            "insufficient_credits",
        ),
        (
            httpx2.Response(400, json={"code": 2008, "message": "Content policy violation"}),
            "content_rejected",
        ),
        (
            httpx2.Response(400, json={"code": 1004, "message": "Invalid parameter"}),
            "invalid_input",
        ),
    ],
)
def test_tripo_submission_errors(
    cloud_client: TestClient, tripo: FakeTripo, response: httpx2.Response, code: str
) -> None:
    tripo.overrides[("POST", "/generation/image-to-model")] = response
    project_id, ids = _project_with_photos(cloud_client, 1)
    job = _run(cloud_client, project_id, ids, provider="tripo")
    assert job["status"] == "failed"
    assert job["error"]["code"] == code
    assert job["provider_task_id"] is None


def test_tripo_task_failure_reports_policy_rejection(
    cloud_client: TestClient, tripo: FakeTripo
) -> None:
    tripo.overrides[("GET", "/tasks/task-1")] = httpx2.Response(
        200,
        json={
            "code": 0,
            "data": {
                "task_id": "task-1",
                "status": "failed",
                "error_code": 2008,
                "error_message": "Image violates content policy",
            },
        },
    )
    project_id, ids = _project_with_photos(cloud_client, 1)
    job = _run(cloud_client, project_id, ids, provider="tripo")
    assert job["status"] == "failed"
    assert job["error"]["code"] == "content_rejected"
    assert job["error"]["details"]["provider_task_id"] == "task-1"


def test_tripo_rate_limit_uses_retry_after() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            429, headers={"Retry-After": "12"}, json={"code": 2000, "message": "Too many tasks"}
        )

    provider = TripoProvider(api_key=TRIPO_KEY, transport=httpx2.MockTransport(handler))
    with pytest.raises(ProviderError) as info:
        provider.poll("task-1")
    assert info.value.code == ProviderErrorCode.RATE_LIMITED
    assert info.value.retry_after == 12.0
    assert info.value.retryable is True


def test_tripo_lost_connection_during_submit_is_not_retried(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/files"):
            return httpx2.Response(200, json={"code": 0, "data": {"file_token": "file_0"}})
        raise httpx2.ReadTimeout("timed out", request=request)

    image = tmp_path / "in.jpg"
    image.write_bytes(images.encode(images.textured(), "JPEG"))
    provider = TripoProvider(api_key=TRIPO_KEY, transport=httpx2.MockTransport(handler))
    request = GenerationRequest(
        job_id=uuid.uuid4(),
        kind=JobKind.IMAGE_TO_3D,
        quality_mode=QualityMode.FAST,
        inputs=(ProviderInput(image, "image/jpeg", "source", "x"),),
        settings={},
    )
    with pytest.raises(ProviderError) as info:
        provider.submit(request)
    # The task may exist (and be billed); never resubmit automatically.
    assert info.value.retryable is False
    assert info.value.details["submission_uncertain"] is True


def test_tripo_has_no_cancel_api() -> None:
    provider = TripoProvider(
        api_key=TRIPO_KEY, transport=httpx2.MockTransport(lambda r: httpx2.Response(500))
    )
    assert provider.cancel("task-1") == "unsupported"
    assert TripoProvider.capabilities().cancel == "unsupported"


# -- Meshy ----------------------------------------------------------------------


def test_meshy_single_image_pipeline(cloud_client: TestClient, meshy: FakeMeshy) -> None:
    project_id, ids = _project_with_photos(cloud_client, 1)
    job = _run(cloud_client, project_id, ids, provider="meshy", quality_mode="professional")
    assert job["status"] == "succeeded", job["error"]
    assert job["provider_task_id"] == "image-to-3d:mesh-task-1"
    assert job["cost"] == {"credits": 30.0, "usd": None, "source": "provider"}
    assert job["estimate"]["usd"] is None  # USD per credit depends on the Meshy plan

    body = meshy.calls("POST", "/image-to-3d")[0].json()
    assert body["image_url"].startswith("data:image/jpeg;base64,")
    sent = base64.b64decode(body["image_url"].split(",", 1)[1])
    with Image.open(io.BytesIO(sent)) as img:
        assert not img.getexif()
    assert {k: v for k, v in body.items() if k != "image_url"} == {
        "ai_model": "latest",
        "should_texture": True,
        "enable_pbr": True,
        "texture_resolution": "4k",
        "geometry_resolution": "2k",
        "target_formats": ["glb"],
    }
    cdn = [r for r in meshy.requests if r.url.startswith(CDN)]
    assert cdn
    assert all("authorization" not in r.headers for r in cdn)


def test_meshy_multi_image_orders_front_first(cloud_client: TestClient, meshy: FakeMeshy) -> None:
    project_id, ids = _project_with_photos(cloud_client, 2)
    response = cloud_client.post(
        f"/api/v1/projects/{project_id}/generations",
        json={
            "inputs": [
                {"image_id": ids[0], "role": "back"},
                {"image_id": ids[1], "role": "front"},
            ],
            "provider": "meshy",
            "cloud_upload_consent": True,
        },
    )
    assert response.status_code == 201
    cloud_client.app.state.runner.run_until_idle()  # type: ignore[attr-defined]
    job = cloud_client.get(f"/api/v1/jobs/{response.json()['id']}").json()
    assert job["status"] == "succeeded", job["error"]
    assert job["provider_task_id"] == "multi-image-to-3d:mesh-task-1"
    urls = meshy.calls("POST", "/multi-image-to-3d")[0].json()["image_urls"]
    first = cloud_client.get(f"/api/v1/images/{ids[1]}").json()
    with Image.open(io.BytesIO(base64.b64decode(urls[0].split(",", 1)[1]))) as front:
        assert front.size[0] == first["width"]


@pytest.mark.parametrize(
    ("status", "message", "code"),
    [
        (401, "Invalid API key", "auth_failed"),
        (402, "Insufficient credits", "insufficient_credits"),
        (403, "Request blocked by moderation", "content_rejected"),
        (400, "Invalid image", "invalid_input"),
        (503, "Service unavailable", "provider_unavailable"),
    ],
)
def test_meshy_submission_errors(
    cloud_client: TestClient, meshy: FakeMeshy, status: int, message: str, code: str
) -> None:
    meshy.overrides[("POST", "/image-to-3d")] = httpx2.Response(status, json={"message": message})
    project_id, ids = _project_with_photos(cloud_client, 1)
    job = _run(cloud_client, project_id, ids, provider="meshy")
    assert job["status"] == "failed"
    assert job["error"]["code"] == code


def test_meshy_task_error_moderation(cloud_client: TestClient, meshy: FakeMeshy) -> None:
    meshy.overrides[("GET", "/mesh-task-1")] = httpx2.Response(
        200,
        json={
            "id": "mesh-task-1",
            "status": "FAILED",
            "progress": 0,
            "consumed_credits": 0,
            "task_error": {
                "type": "invalid_input",
                "code": "moderation_blocked",
                "message": "Blocked",
            },
        },
    )
    project_id, ids = _project_with_photos(cloud_client, 1)
    job = _run(cloud_client, project_id, ids, provider="meshy")
    assert job["error"]["code"] == "content_rejected"
    assert job["cost"] == {"credits": 0.0, "usd": None, "source": "provider"}


def test_meshy_cancel_only_refunds_pending_tasks(meshy: FakeMeshy) -> None:
    provider = MeshyProvider(api_key=MESHY_KEY, transport=meshy.transport())
    meshy.pending = True
    assert provider.cancel("image-to-3d:mesh-task-1") == "cancelled"
    assert meshy.deleted == ["/image-to-3d/mesh-task-1"]

    meshy.pending = False
    meshy.running_polls = 100
    assert provider.cancel("image-to-3d:mesh-task-1") == "too_late"
    assert len(meshy.deleted) == 1

    meshy.pending = True
    meshy.overrides[("DELETE", "/mesh-task-1")] = httpx2.Response(
        409, json={"message": "IN_PROGRESS"}
    )
    assert provider.cancel("image-to-3d:mesh-task-1") == "too_late"


def test_meshy_rejects_foreign_task_reference() -> None:
    provider = MeshyProvider(
        api_key=MESHY_KEY, transport=httpx2.MockTransport(lambda r: httpx2.Response(500))
    )
    with pytest.raises(ProviderError):
        provider.poll("../../balance")


# -- key checks -------------------------------------------------------------------


def test_check_keys_with_balance_calls(cloud_client: TestClient) -> None:
    tripo = cloud_client.post("/api/v1/providers/tripo/check").json()
    assert tripo["ok"] is True
    assert tripo["balance"] == 1234.5
    meshy = cloud_client.post("/api/v1/providers/meshy/check").json()
    assert meshy == {
        "ok": True,
        "message": "Key accepted. Balance: 1000 credits.",
        "balance": 1000.0,
        "unit": "credits",
    }


def test_check_reports_rejected_key(tmp_path: Path, database_url: str, tripo: FakeTripo) -> None:
    settings = make_settings(tmp_path, database_url, tripo_api_key="tsk_wrong_key_000000")
    app = create_app(settings, provider_transport=tripo.transport())
    with TestClient(app) as client:
        result = client.post("/api/v1/providers/tripo/check").json()
    assert result == {
        "ok": False,
        "message": "Tripo rejected the API key.",
        "balance": None,
        "unit": None,
    }
