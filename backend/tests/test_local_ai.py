"""Local single-image AI generation (TripoSR): routing, the job, and a real run.

Routing and job tests replace only the engine call (``generate``) with a
stand-in that writes a real GLB, so planning, consent, the worker, validation
and saving all run for real. The last test runs the installed TripoSR engine
on a real phone photo; it needs AI3D_LOCAL_AI_DIR (see docs/LOCAL_GPU_SETUP.md).
"""

import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient

from ai3d.workers import JobRunner
from ai3d.workers import local_generation as worker
from ai3d_engine.local_ai import LocalAICancelledError, LocalAIFailedError, engine

PLAN = "/api/v1/projects/{}/generations/plan"
LOCAL = "local-triposr"


def _plan(client: TestClient, project_id: str, image_id: str, **body: Any) -> dict[str, Any]:
    response = client.post(
        PLAN.format(project_id), json={"inputs": [{"image_id": image_id}], **body}
    )
    assert response.status_code == 200
    return dict(response.json())


def _stand_in(calls: list[dict[str, Any]], *, fail: Exception | None = None) -> Any:
    """Writes a vertex-coloured sphere where the engine would write its GLB."""

    def generate(image: Path, workdir: Path, **options: Any) -> dict[str, Any]:
        calls.append({"image": image, "bytes": image.read_bytes(), **options})
        progress = options["on_progress"]
        for stage in ("starting", "background", "loading", "generating", "meshing"):
            progress(stage, 0.5)
        if fail is not None:
            raise fail
        workdir.mkdir(parents=True, exist_ok=True)
        sphere = trimesh.creation.icosphere(subdivisions=3, radius=0.5)
        sphere.visual.vertex_colors = np.tile([40, 160, 60, 255], (len(sphere.vertices), 1))
        glb = workdir / "model.glb"
        glb.write_bytes(sphere.export(file_type="glb"))
        return {
            "ok": True,
            "device": "cpu",
            "background_removed": True,
            "triangles": len(sphere.faces),
            "timings": {"generating": 1.0, "meshing": 2.0},
            "seconds_wall": 3.0,
            "glb": str(glb),
        }

    return generate


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The engine counts as installed; its run is the stand-in. Returns the calls."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("ai3d.providers.local_ai.check_local_ai", lambda: None)
    monkeypatch.setattr(worker, "generate", _stand_in(calls))
    return calls


def test_without_the_engine_local_mode_explains_how_to_install_it(
    client: TestClient, make_image_project: Any, tmp_path: Path
) -> None:
    engine.configure(tmp_path / "nothing-installed-here")  # whatever this machine has
    project, ids = make_image_project(1)
    plan = _plan(client, project["id"], ids[0], processing_mode="local")
    assert plan["ok"] is False
    issue = plan["blocking_issues"][0]
    assert issue["code"] == "no_local_engine"
    assert "ai3d-local-ai install" in issue["message"]
    plan = _plan(client, project["id"], ids[0], processing_mode="local", provider=LOCAL)
    assert plan["blocking_issues"][0]["code"] == "provider_not_configured"
    assert "not installed" in plan["blocking_issues"][0]["message"]


def test_a_single_photo_stays_local_in_local_and_automatic_mode(
    client: TestClient, make_image_project: Any, installed: list[dict[str, Any]]
) -> None:
    project, ids = make_image_project(1, default_quality_mode="high_quality")
    plan = _plan(client, project["id"], ids[0], processing_mode="local")
    assert plan["ok"] is True
    assert plan["provider"]["key"] == LOCAL
    assert plan["requires_consent"] is False  # nothing is uploaded
    assert plan["provenance"] == "inferred"  # hidden sides are invented
    assert plan["estimate"]["usd"] == 0
    assert plan["provider_settings"] == {"resolution": 256}

    plan = _plan(client, project["id"], ids[0], processing_mode="auto")
    assert plan["provider"]["key"] == LOCAL
    assert any("stays on this computer" in w for w in plan["warnings"])

    # Cloud mode still means cloud (the mock stands for a cloud provider in tests).
    plan = _plan(client, project["id"], ids[0], processing_mode="cloud")
    assert plan["provider"]["key"] == "mock"
    assert plan["requires_consent"] is True


def test_local_generation_job_saves_an_ai_generated_model(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    installed: list[dict[str, Any]],
) -> None:
    project, ids = make_image_project(1)
    response = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": ids[0]}], "processing_mode": "local"},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["status"] == "queued"  # no consent step: nothing leaves the computer
    runner.run_until_idle()

    job = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == "succeeded", job
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert stages[:3] == ["preparing", "starting", "background"]
    assert "validating" in stages
    assert "saving" in stages
    assert any("Nothing was uploaded" in (e["message"] or "") for e in events)

    (call,) = installed
    assert call["resolution"] == 160  # the project's default quality (fast)
    assert call["bytes"][:2] == b"\xff\xd8"  # a metadata-free JPEG copy of the photo
    assert b"Exif" not in call["bytes"][:4096]

    model = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"][0]
    version = model["current_version"]
    assert version["provider_key"] == LOCAL
    assert version["source"] == "generated"
    assert version["surface_provenance"] == "inferred"
    assert client.get(version["file_url"]).content[:4] == b"glTF"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            LocalAIFailedError("No object was found in the photo.", ["trace"]),
            "failed",
            "engine_failed",
        ),
        (LocalAICancelledError(), "cancelled", None),
    ],
)
def test_engine_failures_and_cancellation_end_the_job_cleanly(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: str,
    code: str | None,
) -> None:
    monkeypatch.setattr("ai3d.providers.local_ai.check_local_ai", lambda: None)
    monkeypatch.setattr(worker, "generate", _stand_in([], fail=error))
    project, ids = make_image_project(1)
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": ids[0]}], "processing_mode": "local"},
    ).json()
    runner.run_until_idle()
    job = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == status
    if code:
        assert job["error"]["code"] == code
        assert job["error"]["message"] == "No object was found in the photo."
    assert client.get(f"/api/v1/projects/{project['id']}/models").json()["items"] == []
    # The heavy-work slot was released: another local job can run.
    assert worker.heavy._heavy_local_work.acquire(blocking=False)
    worker.heavy.release()


# -- falling back to another engine -------------------------------------------------


def _failed_job(
    client: TestClient, runner: JobRunner, project_id: str, image_id: str, **body: Any
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/projects/{project_id}/generations",
        json={"inputs": [{"image_id": image_id}], **body},
    )
    assert response.status_code == 201, response.text
    runner.run_until_idle()
    job = client.get(f"/api/v1/jobs/{response.json()['id']}").json()
    assert job["status"] == "failed", job
    return dict(job)


def test_a_failed_local_job_offers_the_cloud_with_consent(
    client: TestClient, runner: JobRunner, make_image_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ai3d.providers.local_ai.check_local_ai", lambda: None)
    monkeypatch.setattr(
        worker, "generate", _stand_in([], fail=LocalAIFailedError("Out of memory.", []))
    )
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _failed_job(client, runner, project["id"], ids[0], processing_mode="auto")
    assert job["provider"]["key"] == LOCAL
    (offer,) = job["alternatives"]
    assert offer["provider"]["key"] == "mock"
    assert offer["provider"]["uploads_images"] is True
    assert "you are asked first" in offer["reason"]

    # Choosing it without consent uploads nothing: the new job waits for permission.
    waiting = client.post(f"/api/v1/jobs/{job['id']}/retry", json={"provider": "mock"}).json()
    assert waiting["status"] == "waiting_for_consent"
    assert waiting["provider"]["key"] == "mock"
    assert waiting["parent_job_id"] == job["id"]
    # With consent it runs in the cloud (the mock stands for a cloud provider in tests).
    retry = client.post(
        f"/api/v1/jobs/{job['id']}/retry", json={"provider": "mock", "cloud_upload_consent": True}
    ).json()
    runner.run_until_idle()
    assert client.get(f"/api/v1/jobs/{retry['id']}").json()["status"] == "succeeded"


def test_local_mode_is_never_offered_the_cloud(
    client: TestClient, runner: JobRunner, make_image_project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ai3d.providers.local_ai.check_local_ai", lambda: None)
    monkeypatch.setattr(
        worker, "generate", _stand_in([], fail=LocalAIFailedError("No object.", []))
    )
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _failed_job(client, runner, project["id"], ids[0], processing_mode="local")
    assert job["alternatives"] == []
    refused = client.post(
        f"/api/v1/jobs/{job['id']}/retry", json={"provider": "mock", "cloud_upload_consent": True}
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["alternatives"] == []


def test_a_failed_cloud_job_offers_the_local_engine(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    installed: list[dict[str, Any]],
) -> None:
    from ai3d.providers.mock import MockProvider, MockScenario  # noqa: PLC0415

    MockProvider.scenario = MockScenario(fail_on="poll")
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = _failed_job(
        client, runner, project["id"], ids[0], provider="mock", cloud_upload_consent=True
    )
    (offer,) = job["alternatives"]
    assert offer["provider"]["key"] == LOCAL
    assert "nothing is uploaded" in offer["reason"]
    retry = client.post(f"/api/v1/jobs/{job['id']}/retry", json={"provider": LOCAL}).json()
    assert retry["status"] == "queued"  # local: no consent step
    assert retry["processing_mode"] == "local"
    runner.run_until_idle()
    assert client.get(f"/api/v1/jobs/{retry['id']}").json()["status"] == "succeeded"
    assert len(installed) == 1  # the local engine ran once


# -- the real engine --------------------------------------------------------------

REAL_ENGINE = os.environ.get("AI3D_LOCAL_AI_DIR")


def _real_photo() -> Path | None:
    """The phone photo that ships with TripoSR's examples (MIT), inside the engine."""
    if not REAL_ENGINE:
        return None
    found = sorted(Path(REAL_ENGINE).glob("src/TripoSR-*/examples/captured.jpeg"))
    return found[0] if found else None


@pytest.mark.slow
@pytest.mark.skipif(
    not REAL_ENGINE or not engine.status(Path(REAL_ENGINE)).installed,
    reason="set AI3D_LOCAL_AI_DIR to an installed engine (ai3d-local-ai install)",
)
def test_real_photo_to_model_with_the_installed_engine(
    client: TestClient, runner: JobRunner, make_project: Any, upload: Any
) -> None:
    photo = _real_photo()
    assert photo is not None, "the engine's TripoSR examples are missing"
    project = make_project(input_mode="single_image", default_quality_mode="high_quality")
    result = upload(project["id"], ("plush.jpg", photo.read_bytes(), "image/jpeg"))
    image_id = result["results"][0]["image"]["id"]
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": image_id}], "processing_mode": "local"},
    ).json()
    started = time.monotonic()
    runner.run_until_idle()
    seconds = time.monotonic() - started
    job = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == "succeeded", job.get("error")

    version = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"][0][
        "current_version"
    ]
    assert version["provider_key"] == LOCAL
    report = version["quality_report"]
    assert report["status"] != "fail", report
    data = client.get(version["file_url"]).content
    mesh = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="glb", force="mesh")
    assert len(mesh.faces) > 20_000
    colours = mesh.visual.vertex_colors[:, :3].astype(float)
    assert colours.std() > 10  # real colours, not one flat value
    extents = mesh.extents
    assert extents[1] == max(extents)  # upright: the plush toy is taller than wide or deep
    assert "NORMAL" in data[:4096].decode("latin-1")  # smooth normals for shading
    # Not mirrored or inside out, and the colours belong where they are (Phase 11 found
    # that the marching-cubes stand-in returned swapped axes): this plush has orange
    # feet, and dark eyes on the front of its head (+Z, glTF's front).
    assert mesh.volume > 0
    assert report["metrics"]["inside_out"] is False
    height = (mesh.vertices[:, 1] - mesh.vertices[:, 1].min()) / np.ptp(mesh.vertices[:, 1])
    depth = (mesh.vertices[:, 2] - mesh.vertices[:, 2].min()) / np.ptp(mesh.vertices[:, 2])
    orange = (colours[:, 0] > 120) & (colours[:, 1] < 0.75 * colours[:, 0]) & (colours[:, 2] < 60)
    assert orange[height < 0.15].mean() > 0.1
    assert orange[height > 0.5].mean() < 0.01
    eyes = (colours.max(axis=1) < 25) & (height > 0.6)
    assert eyes.sum() > 10
    assert depth[eyes].mean() > 0.6

    # Every available export of this real AI model re-opens with its colours.
    formats = client.get("/api/v1/exports/formats").json()["items"]
    for fmt in (f for f in formats if f["available"]):
        export = client.post(
            f"/api/v1/model-versions/{version['id']}/exports", json={"format": fmt["format"]}
        ).json()
        assert export["status"] == "ready", (fmt["format"], export.get("error_message"))
        assert export["validation"]["coloured"] is (fmt["format"] != "stl"), fmt["format"]
    print(f"\nreal TripoSR run: {seconds:.0f} s, {len(mesh.faces):,} triangles")
