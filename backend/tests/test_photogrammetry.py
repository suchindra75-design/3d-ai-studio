"""Local photogrammetry: routing rules, and real COLMAP runs on synthetic photos.

The ``colmap`` tests run COLMAP itself and need AI3D_COLMAP_PATH to point at
COLMAP 4 (see docs/LOCAL_GPU_SETUP.md); without it they are skipped.
"""

import io
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import update

from ai3d.core.timeutil import utcnow
from ai3d.database.session import Database
from ai3d.models import GenerationJob
from ai3d.models.enums import JobKind, JobStatus
from ai3d.providers.local_photogrammetry import LocalPhotogrammetryProvider
from ai3d.providers.mock import MockProvider
from ai3d.workers import JobRunner
from ai3d_engine.reconstruction import check_colmap
from tests import images, scenes

KEY = LocalPhotogrammetryProvider.key
COLMAP = os.environ.get("AI3D_COLMAP_PATH")
needs_colmap = pytest.mark.skipif(
    not COLMAP or check_colmap(Path(COLMAP)) is not None,
    reason="set AI3D_COLMAP_PATH to COLMAP 4 to run the reconstruction tests",
)

MakeProject = Callable[..., dict[str, Any]]


@pytest.fixture(scope="session")
def capture_set(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, bytes]]:
    """30 photos walking around a textured box (rendered once per test session)."""
    return scenes.photo_set()


@pytest.fixture
def engine_available(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def _set(available: bool) -> None:
        monkeypatch.setattr(
            LocalPhotogrammetryProvider,
            "unavailable_reason",
            classmethod(lambda cls, settings: None if available else "COLMAP was not found."),
        )

    return _set


def _photos(client: TestClient, make_project: MakeProject, files: list[tuple[str, bytes]]):
    project = make_project(input_mode="multi_view")
    ids: list[str] = []
    for start in range(0, len(files), 20):
        response = client.post(
            f"/api/v1/projects/{project['id']}/images",
            files=[("files", (n, d, "image/jpeg")) for n, d in files[start : start + 20]],
        )
        assert response.status_code == 200, response.text
        ids += [r["image"]["id"] for r in response.json()["results"]]
    return project, ids


def _distinct_photos(count: int) -> list[tuple[str, bytes]]:
    return [
        (f"p{i}.jpg", images.encode(images.textured(320, 240, seed=100 + i), "JPEG"))
        for i in range(count)
    ]


def _plan(client: TestClient, project_id: str, ids: list[str], **body: Any) -> Any:
    return client.post(
        f"/api/v1/projects/{project_id}/generations/plan",
        json={"inputs": [{"image_id": i} for i in ids], **body},
    ).json()


# -- routing ------------------------------------------------------------------------


@pytest.mark.usefixtures("no_local_ai")
def test_a_single_photo_is_never_sent_to_photogrammetry(
    client: TestClient, make_project: MakeProject, engine_available: Callable[[bool], None]
) -> None:
    engine_available(True)
    project, ids = _photos(client, make_project, _distinct_photos(1))
    local = _plan(client, project["id"], ids, processing_mode="local")
    assert local["ok"] is False
    assert local["blocking_issues"][0]["code"] == "no_local_engine"
    assert (
        "photogrammetry works from many overlapping photos"
        in local["blocking_issues"][0]["message"]
    )
    explicit = _plan(client, project["id"], ids, provider=KEY)
    assert explicit["kind"] == "photogrammetry"
    assert explicit["blocking_issues"][0]["code"] == "too_few_photos"
    assert "cannot recover shape from a single photo" in explicit["blocking_issues"][0]["message"]


def test_local_processing_plans_photogrammetry(
    client: TestClient, make_project: MakeProject, engine_available: Callable[[bool], None]
) -> None:
    engine_available(True)
    project, ids = _photos(client, make_project, _distinct_photos(6))
    plan = _plan(client, project["id"], ids, processing_mode="local", quality_mode="fast")
    assert plan["ok"] is True, plan["blocking_issues"]
    assert plan["kind"] == "photogrammetry"
    assert plan["route"] == "local"
    assert plan["provider"]["key"] == KEY
    assert plan["provider"]["uploads_images"] is False
    assert plan["requires_consent"] is False
    assert plan["provenance"] == "measured"
    assert plan["estimate"]["usd"] == 0
    assert {i["role"] for i in plan["inputs"]} == {"source"}  # views are found, not labelled
    assert plan["provider_settings"]["max_image_size"] == 1600
    warnings = " ".join(plan["warnings"])
    assert "size is not to scale" in warnings
    assert "Only 6 photos" in warnings


def test_missing_colmap_blocks_with_the_reason(
    client: TestClient, make_project: MakeProject, engine_available: Callable[[bool], None]
) -> None:
    engine_available(False)
    project, ids = _photos(client, make_project, _distinct_photos(3))
    plan = _plan(client, project["id"], ids, processing_mode="local")
    assert plan["blocking_issues"] == [
        {"code": "engine_unavailable", "message": "COLMAP was not found."}
    ]
    providers = {p["key"]: p for p in client.get("/api/v1/providers").json()["items"]}
    assert providers[KEY]["configured"] is False
    assert providers[KEY]["unavailable_reason"] == "COLMAP was not found."
    assert providers[KEY]["capabilities"]["photogrammetry"]["min_images"] == 3


def test_automatic_mode_measures_capture_sets_and_keeps_small_sets_on_ai(
    client: TestClient, make_project: MakeProject, engine_available: Callable[[bool], None]
) -> None:
    engine_available(True)
    project, ids = _photos(client, make_project, _distinct_photos(22))
    plan = _plan(client, project["id"], ids, processing_mode="auto")
    assert plan["kind"] == "photogrammetry"
    assert "photos stay on this computer" in " ".join(plan["warnings"])
    # Three photos are better suited to AI multi-view (the mock provider in tests).
    small = _plan(client, project["id"], ids[:3], processing_mode="auto")
    assert small["kind"] == "multi_view_to_3d"
    assert small["route"] == "cloud"


def test_large_sets_need_photogrammetry(
    client: TestClient, make_project: MakeProject, engine_available: Callable[[bool], None]
) -> None:
    engine_available(False)
    project, ids = _photos(client, make_project, _distinct_photos(6))
    response = client.post(
        f"/api/v1/projects/{project['id']}/generations/plan",
        json={"inputs": [{"image_id": i} for i in ids], "processing_mode": "auto"},
    )
    assert response.status_code == 422
    assert "at most 4 photos" in response.json()["error"]["message"]


def test_photogrammetry_jobs_need_no_consent(
    client: TestClient, make_project: MakeProject, engine_available: Callable[[bool], None]
) -> None:
    engine_available(True)
    project, ids = _photos(client, make_project, _distinct_photos(4))
    assert project["allow_cloud_upload"] is False
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": i} for i in ids], "provider": KEY},
    ).json()
    assert job["status"] == "queued"
    assert job["kind"] == "photogrammetry"
    assert job["cloud_upload_consented"] is False
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    assert events[0]["message"] == "Queued for Local photogrammetry (COLMAP)."


def test_interrupted_reconstruction_is_restarted(
    app: FastAPI,
    client: TestClient,
    runner: JobRunner,
    make_project: MakeProject,
    engine_available: Callable[[bool], None],
) -> None:
    engine_available(True)
    project, ids = _photos(client, make_project, _distinct_photos(3))
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": i} for i in ids], "provider": KEY},
    ).json()
    database: Database = app.state.db
    with database.session() as db:
        db.execute(
            update(GenerationJob)
            .where(GenerationJob.id == uuid.UUID(job["id"]))
            .values(
                status=JobStatus.RUNNING,
                stage="mapping",
                lease_owner="dead-worker",
                lease_expires_at=utcnow() - timedelta(seconds=1),
            )
        )
    assert runner.recover() == 1
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "queued"
    last = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"][-1]
    assert last["message"].startswith("Restarting the local reconstruction")


# -- real COLMAP runs --------------------------------------------------------------------


@needs_colmap
@pytest.mark.slow
def test_reconstructs_a_textured_measured_model(
    client: TestClient,
    runner: JobRunner,
    make_project: MakeProject,
    capture_set: list[tuple[str, bytes]],
    settings: Any,
) -> None:
    project, ids = _photos(client, make_project, capture_set)
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={
            "inputs": [{"image_id": i} for i in ids],
            "processing_mode": "local",
            "quality_mode": "fast",
        },
    ).json()
    assert job["kind"] == "photogrammetry", job
    assert runner.run_until_idle() == 1

    final = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert final["status"] == "succeeded", final["error"]
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    stages = [e["stage"] for e in events if e["type"] == "stage"]
    assert stages == [
        "preparing",
        "features",
        "matching",
        "mapping",
        "filtering",
        "meshing",
        "texturing",
        "validating",
        "saving",
    ]
    progress = [e["progress"] for e in events if e["progress"] is not None]
    assert progress == sorted(progress)  # never moves backwards
    assert MockProvider.submissions == []  # nothing went to a provider

    model = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"][0]
    version = model["current_version"]
    assert model["name"] == "Photogrammetry · 30 photos"
    assert version["source"] == "reconstructed"
    assert version["surface_provenance"] == "measured"
    assert "not to scale" in version["notes"]
    report = version["quality_report"]
    recon = report["metrics"]["reconstruction"]
    assert recon["registered_images"] == 30
    assert recon["textured"] is True
    assert recon["mean_reprojection_error_px"] < 1.0
    assert recon["sparse_points"] > 1000
    assert report["metrics"]["texture_count"] == 1
    # Topology is judged on the shape, not on texture seams.
    assert report["metrics"]["components"] < 20

    glb = client.get(version["file_url"]).content
    scene = trimesh.load_scene(io.BytesIO(glb), file_type="glb")
    extents = scene.extents
    # Upright: the floor (3 m wide) is level and the box (0.7 m tall) stands up from it.
    assert extents[1] < 0.5 * min(extents[0], extents[2])
    assert scene.bounds[0][1] == pytest.approx(0, abs=0.1 * extents[1])

    # The photos never left the job's private folder, which is gone afterwards.
    assert list((settings.data_dir / "tmp").glob("reconstruct-*")) == []


@needs_colmap
@pytest.mark.slow
def test_photos_that_do_not_overlap_fail_with_an_explanation(
    client: TestClient, runner: JobRunner, make_project: MakeProject
) -> None:
    project, ids = _photos(client, make_project, _distinct_photos(5))
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": i} for i in ids], "provider": KEY},
    ).json()
    runner.run_until_idle()
    final = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert final["status"] == "failed"
    assert final["error"]["code"] in {"no_reconstruction", "too_few_registered"}
    assert "overlap" in final["error"]["message"]
    assert client.get(f"/api/v1/projects/{project['id']}/models").json()["total"] == 0


def _colmap_processes() -> list[psutil.Process]:
    found = []
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if (process.info["name"] or "") == "colmap":
                found.append(process)
        except psutil.Error:
            continue
    return found


@pytest.fixture
def background(runner: JobRunner) -> Iterator[Callable[[str], threading.Thread]]:
    threads: list[threading.Thread] = []

    def _start(job_id: str) -> threading.Thread:
        claimed = runner.claim_next()
        assert claimed == uuid.UUID(job_id)
        thread = threading.Thread(target=runner.execute, args=(claimed,), daemon=True)
        thread.start()
        threads.append(thread)
        return thread

    yield _start
    for thread in threads:
        thread.join(timeout=60)


@needs_colmap
@pytest.mark.slow
def test_cancelling_stops_colmap(
    client: TestClient,
    make_project: MakeProject,
    capture_set: list[tuple[str, bytes]],
    background: Callable[[str], threading.Thread],
) -> None:
    before = {p.pid for p in _colmap_processes()}
    project, ids = _photos(client, make_project, capture_set)
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": i} for i in ids], "provider": KEY},
    ).json()
    thread = background(job["id"])
    deadline = time.monotonic() + 60
    while client.get(f"/api/v1/jobs/{job['id']}").json()["stage"] not in {
        "features",
        "matching",
        "mapping",
    }:
        assert time.monotonic() < deadline, "COLMAP did not start"
        time.sleep(0.2)
    assert client.post(f"/api/v1/jobs/{job['id']}/cancel").status_code == 200
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "cancelled"
    leftover = [p for p in _colmap_processes() if p.pid not in before and p.is_running()]
    assert [p for p in leftover if p.status() != psutil.STATUS_ZOMBIE] == []


def test_job_kind_enum_value() -> None:
    assert JobKind.PHOTOGRAMMETRY.value == "photogrammetry"
    assert np.__version__  # numpy is an engine dependency now
    assert Image.__name__
