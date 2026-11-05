"""End-to-end generation pipeline with the test-only mock provider."""

import io
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import update

from ai3d.core.timeutil import utcnow
from ai3d.database.session import Database
from ai3d.models import GenerationJob
from ai3d.models.enums import JobStatus
from ai3d.providers.base import ProviderErrorCode
from ai3d.providers.mock import MockProvider, MockScenario
from ai3d.workers import JobRunner
from tests import images
from tests.conftest import stored_keys

MakeImageProject = Callable[..., tuple[dict[str, Any], list[str]]]


def _generate(client: TestClient, project_id: str, image_ids: list[str], **body: Any) -> Any:
    payload = {
        "inputs": [{"image_id": i} for i in image_ids],
        "provider": "mock",
        "cloud_upload_consent": True,
        **body,
    }
    return client.post(f"/api/v1/projects/{project_id}/generations", json=payload)


def _job(client: TestClient, job_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    return dict(response.json())


def _events(client: TestClient, job_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/api/v1/jobs/{job_id}/events").json()["items"])


# -- planning -----------------------------------------------------------------


def test_plan_single_image_is_honest_about_inferred_geometry(
    client: TestClient, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, input_mode="single_image")
    plan = client.post(
        f"/api/v1/projects/{project['id']}/generations/plan",
        json={"inputs": [{"image_id": ids[0]}], "provider": "mock"},
    ).json()
    assert plan["ok"] is True
    assert plan["kind"] == "image_to_3d"
    assert plan["provider"]["key"] == "mock"
    assert plan["provenance"] == "inferred"
    assert plan["requires_consent"] is True
    assert plan["consent_reason"] == "project_local_only"
    assert "invented" in plan["warnings"][0]
    assert plan["estimate"]["amount"] == 0
    assert plan["inputs"][0]["role"] == "source"


@pytest.mark.usefixtures("no_local_ai")
def test_plan_local_processing_never_routes_to_cloud(
    client: TestClient, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1)
    body = {"inputs": [{"image_id": ids[0]}], "processing_mode": "local"}
    plan = client.post(f"/api/v1/projects/{project['id']}/generations/plan", json=body).json()
    assert plan["ok"] is False
    assert plan["provider"] is None
    assert plan["blocking_issues"][0]["code"] == "no_local_engine"

    plan = client.post(
        f"/api/v1/projects/{project['id']}/generations/plan", json={**body, "provider": "mock"}
    ).json()
    assert plan["blocking_issues"][0]["code"] == "local_only"
    response = _generate(client, project["id"], ids, processing_mode="local")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "local_only"


@pytest.mark.usefixtures("no_local_ai")
def test_auto_mode_says_it_chose_the_cloud(
    client: TestClient, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1)
    plan = client.post(
        f"/api/v1/projects/{project['id']}/generations/plan",
        json={"inputs": [{"image_id": ids[0]}], "processing_mode": "auto"},
    ).json()
    # Tripo and Meshy have no key in tests, so the only configured provider is the mock.
    assert plan["provider"]["key"] == "mock"
    assert any("Automatic mode chose" in w for w in plan["warnings"])


def test_unconfigured_provider_is_blocked(
    client: TestClient, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1)
    response = _generate(client, project["id"], ids, provider="tripo")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_not_configured"


def test_multi_view_roles(client: TestClient, make_image_project: MakeImageProject) -> None:
    project, ids = make_image_project(3, allow_cloud_upload=True)
    plan = client.post(
        f"/api/v1/projects/{project['id']}/generations/plan",
        json={"inputs": [{"image_id": i} for i in ids], "provider": "mock"},
    ).json()
    assert plan["kind"] == "multi_view_to_3d"
    assert [i["role"] for i in plan["inputs"]] == ["front", "left", "back"]

    duplicate = [{"image_id": ids[0], "role": "front"}, {"image_id": ids[1], "role": "front"}]
    response = client.post(
        f"/api/v1/projects/{project['id']}/generations/plan", json={"inputs": duplicate}
    )
    assert response.status_code == 422

    no_front = [{"image_id": ids[0], "role": "left"}, {"image_id": ids[1], "role": "back"}]
    plan = client.post(
        f"/api/v1/projects/{project['id']}/generations/plan",
        json={"inputs": no_front, "provider": "mock"},
    ).json()
    assert plan["blocking_issues"][0]["code"] == "missing_view"


def test_photos_from_another_project_are_rejected(
    client: TestClient, make_image_project: MakeImageProject
) -> None:
    project, _ids = make_image_project(1)
    _other, other_ids = make_image_project(1)
    response = _generate(client, project["id"], other_ids)
    assert response.status_code == 404


# -- consent ------------------------------------------------------------------


def test_nothing_is_uploaded_without_consent(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    response = _generate(client, project["id"], ids, cloud_upload_consent=False)
    assert response.status_code == 201
    job = response.json()
    assert job["status"] == "waiting_for_consent"
    assert job["consent_reason"] == "confirmation_required"
    assert runner.run_until_idle() == 0
    assert MockProvider.submissions == []
    assert "Nothing has been uploaded" in _events(client, job["id"])[0]["message"]

    granted = client.post(f"/api/v1/jobs/{job['id']}/consent", json={})
    assert granted.status_code == 200
    assert granted.json()["status"] == "queued"
    assert runner.run_until_idle() == 1
    assert _job(client, job["id"])["status"] == "succeeded"
    assert len(MockProvider.submissions) == 1


def test_local_only_project_requires_explicit_project_permission(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1)  # allow_cloud_upload defaults to False
    # Even with per-job consent, a local-only project's job waits.
    job = _generate(client, project["id"], ids).json()
    assert job["status"] == "waiting_for_consent"
    assert job["consent_reason"] == "project_local_only"

    refused = client.post(f"/api/v1/jobs/{job['id']}/consent", json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "project_local_only"
    assert client.get(f"/api/v1/projects/{project['id']}").json()["allow_cloud_upload"] is False

    granted = client.post(
        f"/api/v1/jobs/{job['id']}/consent", json={"allow_project_cloud_upload": True}
    )
    assert granted.json()["status"] == "queued"
    assert client.get(f"/api/v1/projects/{project['id']}").json()["allow_cloud_upload"] is True
    runner.run_until_idle()
    assert _job(client, job["id"])["status"] == "succeeded"


def test_declining_consent_cancels(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1)
    job = _generate(client, project["id"], ids).json()
    cancelled = client.post(f"/api/v1/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert runner.run_until_idle() == 0
    assert MockProvider.submissions == []


# -- the full pipeline ----------------------------------------------------------


def test_full_pipeline_creates_validated_model(
    client: TestClient, runner: JobRunner, make_project: Any, upload: Any
) -> None:
    project = make_project(input_mode="single_image", allow_cloud_upload=True)
    photo = images.jpeg_with_exif(images.textured(3000, 2000), orientation=6, gps=True)
    image_id = upload(project["id"], ("phone.jpg", photo, "image/jpeg"))["results"][0]["image"][
        "id"
    ]

    job = _generate(client, project["id"], [image_id], quality_mode="high_quality").json()
    assert job["status"] == "queued"
    assert job["provider_summary"] == ["Placeholder sphere (test provider)"]
    assert runner.run_until_idle() == 1

    job = _job(client, job["id"])
    assert job["status"] == "succeeded", job["error"]
    assert job["progress"] == 1.0
    assert job["provider_task_id"].startswith("mock-")
    assert job["cost"] == {"credits": 0.0, "usd": 0.0, "source": "provider"}
    assert job["result_model_id"] is not None

    # What left the machine: an upright, resized copy without any metadata.
    sent = MockProvider.submissions[0]
    assert sent.settings == {"shape": "icosphere", "subdivisions": 3}
    with Image.open(io.BytesIO(sent.input_bytes[0])) as copy:
        assert copy.format == "JPEG"
        assert copy.size == (1365, 2048)  # EXIF orientation 6 applied, long side 2048
        assert not copy.getexif()
        assert "exif" not in copy.info
        assert "icc_profile" not in copy.info
    assert b"TestCam" not in sent.input_bytes[0]

    # The original upload is untouched and the provider copy is not listed as a photo.
    original = client.get(f"/api/v1/images/{image_id}/file").content
    assert original == photo
    assert len(client.get(f"/api/v1/projects/{project['id']}/images").json()["items"]) == 1

    stages = [e["stage"] for e in _events(client, job["id"]) if e["type"] == "stage"]
    assert stages[:3] == ["preparing", "submitting", "generating"]
    assert stages[-3:] == ["downloading", "validating", "saving"]

    models = client.get(f"/api/v1/projects/{project['id']}/models").json()
    assert models["total"] == 1
    version = models["items"][0]["current_version"]
    assert version["surface_provenance"] == "inferred"
    assert version["provider_key"] == "mock"
    assert "not a reconstruction" in version["notes"]
    assert version["quality_report"]["status"] == "pass"
    assert version["stats"]["triangle_count"] == 1280
    assert version["stats"]["watertight"] is True

    download = client.get(version["file_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "model/gltf-binary"
    assert "attachment" in download.headers["content-disposition"]
    scene = trimesh.load_scene(io.BytesIO(download.content), file_type="glb")
    assert sum(len(g.faces) for g in scene.geometry.values()) == 1280

    preview = client.get(version["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/webp"


def test_provider_copy_is_cached_between_jobs(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    _generate(client, project["id"], ids)
    _generate(client, project["id"], ids)
    assert runner.run_until_idle() == 2
    first, second = MockProvider.submissions
    assert first.input_bytes == second.input_bytes


def test_multi_view_pipeline(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(4, allow_cloud_upload=True)
    roles = ["back", "front", "right", "left"]
    response = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={
            "inputs": [{"image_id": i, "role": r} for i, r in zip(ids, roles, strict=True)],
            "provider": "mock",
            "cloud_upload_consent": True,
        },
    )
    assert response.status_code == 201
    runner.run_until_idle()
    assert _job(client, response.json()["id"])["status"] == "succeeded"
    assert MockProvider.submissions[0].kind == "multi_view_to_3d"
    assert MockProvider.submissions[0].roles == roles


def test_idempotency_key_returns_the_same_job(
    client: TestClient, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    payload = {"inputs": [{"image_id": ids[0]}], "provider": "mock", "cloud_upload_consent": True}
    url = f"/api/v1/projects/{project['id']}/generations"
    first = client.post(url, json=payload, headers={"Idempotency-Key": "abc-123"})
    second = client.post(url, json=payload, headers={"Idempotency-Key": "abc-123"})
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get(f"/api/v1/projects/{project['id']}/jobs").json()["items"]) == 1


# -- failures, retries, cancellation ------------------------------------------


def test_provider_failure_and_retry(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    MockProvider.scenario = MockScenario(
        fail_on="poll", error_code=ProviderErrorCode.CONTENT_REJECTED
    )
    job = _generate(client, project["id"], ids).json()
    runner.run_until_idle()
    job = _job(client, job["id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "content_rejected"
    assert job["error"]["details"]["provider_task_id"].startswith("mock-")
    assert job["can_retry"] is True
    assert _events(client, job["id"])[-1]["type"] == "error"

    MockProvider.scenario = MockScenario()
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry", json={"cloud_upload_consent": True})
    assert retry.status_code == 201
    assert retry.json()["parent_job_id"] == job["id"]
    runner.run_until_idle()
    assert _job(client, retry.json()["id"])["status"] == "succeeded"

    # Retrying without consent waits again instead of uploading.
    again = client.post(f"/api/v1/jobs/{job['id']}/retry", json={}).json()
    assert again["status"] == "waiting_for_consent"


def test_mock_can_fail_the_first_task_for_a_photo_set(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    # Used by browser tests that run the server as a separate process.
    project, ids = make_image_project(1, allow_cloud_upload=True)
    MockProvider.scenario = MockScenario(fail_first_task=True)
    job = _generate(client, project["id"], ids).json()
    runner.run_until_idle()
    failed = _job(client, job["id"])
    assert failed["status"] == "failed"
    assert failed["error"]["message"] == "Mock provider simulated task_failed."
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry", json={"cloud_upload_consent": True})
    runner.run_until_idle()
    assert _job(client, retry.json()["id"])["status"] == "succeeded"


def test_rate_limited_submission_is_retried_then_fails(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    MockProvider.scenario = MockScenario(
        fail_on="submit", error_code=ProviderErrorCode.RATE_LIMITED, retry_after=0
    )
    job = _generate(client, project["id"], ids).json()
    runner.run_until_idle()
    job = _job(client, job["id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "rate_limited"
    warnings = [e for e in _events(client, job["id"]) if e["type"] == "warning"]
    assert len(warnings) == 3  # three automatic retries before giving up


def test_invalid_output_is_rejected(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    MockProvider.scenario = MockScenario(output="garbage")
    job = _generate(client, project["id"], ids).json()
    runner.run_until_idle()
    job = _job(client, job["id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "invalid_output"
    assert client.get(f"/api/v1/projects/{project['id']}/models").json()["total"] == 0


def test_deleted_photo_fails_the_job(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _generate(client, project["id"], ids).json()
    assert client.delete(f"/api/v1/images/{ids[0]}").status_code == 204
    runner.run_until_idle()
    job = _job(client, job["id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "input_missing"
    assert MockProvider.submissions == []
    assert client.post(f"/api/v1/jobs/{job['id']}/retry", json={}).status_code == 422


def test_cancel_while_running(
    app: FastAPI,
    client: TestClient,
    runner: JobRunner,
    make_image_project: MakeImageProject,
) -> None:
    app.state.settings.provider_poll_interval = 0.02
    project, ids = make_image_project(1, allow_cloud_upload=True)
    MockProvider.scenario = MockScenario(polls_until_done=10**6)
    job = _generate(client, project["id"], ids).json()
    job_id = runner.claim_next()
    assert str(job_id) == job["id"]
    worker = threading.Thread(target=runner.execute, args=(job_id,))
    worker.start()
    deadline = time.monotonic() + 10
    while _job(client, job["id"])["provider_task_id"] is None:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    cancelled = client.post(f"/api/v1/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelling"
    worker.join(10)
    assert not worker.is_alive()
    final = _job(client, job["id"])
    assert final["status"] == "cancelled"
    assert final["can_retry"] is True
    assert "cancelled the task" in _events(client, job["id"])[-1]["message"]


def test_cancel_unsupported_by_provider_is_reported(
    app: FastAPI,
    client: TestClient,
    runner: JobRunner,
    make_image_project: MakeImageProject,
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    MockProvider.scenario = MockScenario(polls_until_done=10**6, cancel_outcome="unsupported")
    job = _generate(client, project["id"], ids).json()
    job_id = runner.claim_next()
    assert job_id is not None
    # Simulate the cancel arriving after submission: the worker sees it at its next heartbeat.
    database: Database = app.state.db
    original_poll = MockProvider.poll

    def poll_then_cancel(self: MockProvider, task_id: str) -> Any:
        with database.session() as db:
            db.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(cancel_requested=True, status=JobStatus.CANCELLING)
            )
        return original_poll(self, task_id)

    MockProvider.poll = poll_then_cancel  # type: ignore[method-assign]
    try:
        runner.execute(job_id)
    finally:
        MockProvider.poll = original_poll  # type: ignore[method-assign]
    final = _job(client, job["id"])
    assert final["status"] == "cancelled"
    last = _events(client, job["id"])[-1]
    assert last["type"] == "warning"
    assert "may still finish and be charged" in last["message"]


def test_cannot_cancel_finished_job(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _generate(client, project["id"], ids).json()
    runner.run_until_idle()
    assert client.post(f"/api/v1/jobs/{job['id']}/cancel").status_code == 409


# -- restart recovery -------------------------------------------------------------


def _expire_lease(app: FastAPI, job_id: str, **values: Any) -> None:
    database: Database = app.state.db
    with database.session() as db:
        db.execute(
            update(GenerationJob)
            .where(GenerationJob.id == uuid.UUID(job_id))
            .values(
                status=JobStatus.RUNNING,
                lease_owner="dead-worker",
                lease_expires_at=utcnow() - timedelta(seconds=1),
                **values,
            )
        )


def test_recovery_resumes_existing_provider_task(
    app: FastAPI, client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _generate(client, project["id"], ids).json()
    _expire_lease(app, job["id"], provider_task_id="mock-from-before-restart", stage="generating")

    assert runner.recover() == 1
    assert _job(client, job["id"])["status"] == "queued"
    assert "nothing is uploaded or charged again" in _events(client, job["id"])[-1]["message"]
    runner.run_until_idle()
    final = _job(client, job["id"])
    assert final["status"] == "succeeded"
    assert final["provider_task_id"] == "mock-from-before-restart"
    assert MockProvider.submissions == []  # resumed, not resubmitted


def test_recovery_never_resubmits_an_uncertain_submission(
    app: FastAPI, client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _generate(client, project["id"], ids).json()
    _expire_lease(app, job["id"], stage="submitting")
    assert runner.recover() == 1
    final = _job(client, job["id"])
    assert final["status"] == "failed"
    assert final["error"]["code"] == "interrupted"
    assert "dashboard" in final["error"]["message"]


def test_recovery_requeues_jobs_that_sent_nothing(
    app: FastAPI, client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _generate(client, project["id"], ids).json()
    _expire_lease(app, job["id"], stage="preparing")
    assert runner.recover() == 1
    runner.run_until_idle()
    assert _job(client, job["id"])["status"] == "succeeded"


def test_live_lease_is_not_recovered(
    app: FastAPI, client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _generate(client, project["id"], ids).json()
    assert runner.claim_next() is not None
    assert runner.recover() == 0
    assert _job(client, job["id"])["status"] == "running"


def test_background_runner_processes_jobs(
    app: FastAPI, client: TestClient, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    runner: JobRunner = app.state.runner
    runner.start()
    try:
        job = _generate(client, project["id"], ids).json()
        deadline = time.monotonic() + 15
        while _job(client, job["id"])["status"] != "succeeded":
            assert time.monotonic() < deadline, _job(client, job["id"])
            time.sleep(0.05)
    finally:
        runner.stop()


# -- event stream -----------------------------------------------------------------


def test_event_stream(
    client: TestClient, runner: JobRunner, make_image_project: MakeImageProject
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _generate(client, project["id"], ids).json()
    runner.run_until_idle()
    events = _events(client, job["id"])

    with client.stream("GET", f"/api/v1/jobs/{job['id']}/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Proxies must not compress (and so buffer) the stream.
        assert "no-transform" in response.headers["cache-control"]
        text = "".join(response.iter_text())
    assert f"id: {events[-1]['seq']}\n" in text
    assert "event: stage\n" in text
    assert text.rstrip().endswith('data: {"status": "succeeded"}')

    with client.stream(
        "GET",
        f"/api/v1/jobs/{job['id']}/stream",
        headers={"Last-Event-ID": str(events[-2]["seq"])},
    ) as response:
        resumed = "".join(response.iter_text())
    assert resumed.count("id: ") == 1
    assert client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000/stream").status_code == 404


# -- models -------------------------------------------------------------------------


def test_delete_model_and_project_with_models(
    client: TestClient,
    runner: JobRunner,
    make_image_project: MakeImageProject,
    settings: Any,
) -> None:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    _generate(client, project["id"], ids)
    _generate(client, project["id"], ids)
    runner.run_until_idle()
    models = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"]
    assert len(models) == 2
    model_dir = f"projects/{project['id']}/models/{models[0]['id']}/"
    assert stored_keys(client, model_dir)

    assert client.delete(f"/api/v1/models/{models[0]['id']}").status_code == 204
    assert stored_keys(client, model_dir) == []
    assert client.get(f"/api/v1/models/{models[0]['id']}").status_code == 404
    # The job keeps its history; the link to the deleted version is cleared.
    jobs = client.get(f"/api/v1/projects/{project['id']}/jobs").json()["items"]
    assert sum(j["result_model_version_id"] is None for j in jobs) == 1

    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    assert stored_keys(client, f"projects/{project['id']}/") == []


@pytest.mark.parametrize("host", ["evil.example", "192.168.1.20:8000"])
def test_unknown_host_header_is_rejected(client: TestClient, host: str) -> None:
    # Protects the local API (and stored provider keys) from DNS-rebinding pages.
    response = client.get("/api/v1/providers", headers={"Host": host})
    assert response.status_code == 400
