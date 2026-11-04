"""Memory and disk checks before heavy local jobs, and what happens when they fail."""

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai3d.models.enums import QualityMode
from ai3d.processing import geometry
from ai3d.services import resources
from ai3d.workers import JobRunner
from ai3d.workers import local_generation as worker
from tests.test_blender import _scene, _texture, _textured_sphere
from tests.test_local_ai import LOCAL, _plan, _stand_in
from tests.test_model_versions import _generated_model, _save

GB = resources.GB


def _machine(monkeypatch: pytest.MonkeyPatch, *, disk: float, total: float, free: float) -> None:
    here = resources.Machine(int(disk * GB), int(total * GB), int(free * GB))
    monkeypatch.setattr(resources, "machine", lambda _data_dir: here)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("ai3d.providers.local_ai.check_local_ai", lambda: None)
    monkeypatch.setattr(worker, "generate", _stand_in(calls))
    return calls


def test_a_computer_too_small_for_local_ai_uses_the_cloud_in_automatic_mode(
    client: TestClient,
    make_image_project: Any,
    engine: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machine(monkeypatch, disk=100, total=4, free=3)
    project, ids = make_image_project(1, allow_cloud_upload=True)
    plan = _plan(client, project["id"], ids[0], processing_mode="auto")
    assert plan["provider"]["key"] == "mock"  # the cloud, with the usual consent
    assert any("at least 8.0 GB of memory" in w for w in plan["warnings"])
    local = _plan(client, project["id"], ids[0], processing_mode="local")
    assert local["blocking_issues"][0]["code"] == "machine_too_small"
    asked = _plan(client, project["id"], ids[0], provider=LOCAL)
    assert asked["blocking_issues"][0]["code"] == "machine_too_small"


def test_too_little_disk_blocks_and_low_memory_warns(
    client: TestClient,
    make_image_project: Any,
    engine: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, ids = make_image_project(1)
    _machine(monkeypatch, disk=0.2, total=16, free=12)
    plan = _plan(client, project["id"], ids[0], processing_mode="local")
    issue = plan["blocking_issues"][0]
    assert issue["code"] == "not_enough_disk"
    assert "1.0 GB of free disk space" in issue["message"]
    assert "0.2 GB is free" in issue["message"]

    _machine(monkeypatch, disk=100, total=16, free=2)
    plan = _plan(client, project["id"], ids[0], processing_mode="local")
    assert plan["ok"] is True
    assert any("close other programs" in w for w in plan["warnings"])


def test_a_disk_that_fills_up_while_waiting_fails_the_job_cleanly(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    engine: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machine(monkeypatch, disk=100, total=16, free=12)
    project, ids = make_image_project(1, allow_cloud_upload=True)
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": ids[0]}], "processing_mode": "auto"},
    ).json()
    _machine(monkeypatch, disk=0.1, total=16, free=12)
    runner.run_until_idle()
    job = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == "failed"
    assert job["error"]["code"] == "not_enough_disk"
    assert engine == []  # the engine never started
    assert [a["provider"]["key"] for a in job["alternatives"]] == ["mock"]
    # The heavy-work slot was released.
    assert worker.heavy._heavy_local_work.acquire(blocking=False)
    worker.heavy.release()


def test_photogrammetry_needs_three_times_the_photos_on_disk() -> None:
    small = resources.photogrammetry(100 * 1024**2)
    assert small.disk_bytes == 2 * GB  # at least 2 GB
    large = resources.photogrammetry(3 * GB)
    assert large.disk_bytes == 9 * GB
    here = resources.Machine(int(5 * GB), 16 * GB, 8 * GB)
    problem = resources.not_enough_disk(large, here, "Photogrammetry")
    assert problem is not None
    assert "9.0 GB" in problem
    assert resources.not_enough_disk(small, here, "Photogrammetry") is None


def test_enhance_waits_for_another_heavy_job_before_baking(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baked: list[bool] = []

    def fake_bake(*_args: object, **_kw: object) -> object:
        baked.append(worker.heavy._heavy_local_work._value == 0)  # holds the slot
        step = geometry.Step("decimate", "Reduce", "skipped", "stand-in for Blender")
        return None, step, geometry.Step("baking", "Texture baking", "skipped", "stand-in")

    pro = geometry.PROFILES[QualityMode.PROFESSIONAL]
    monkeypatch.setitem(
        geometry.PROFILES,
        QualityMode.PROFESSIONAL,
        geometry.Profile(
            pro.min_part_fraction, pro.fix_normals, pro.fill_holes, pro.max_texture, 600, True
        ),
    )
    monkeypatch.setattr(geometry, "check_blender", lambda: None)
    monkeypatch.setattr(geometry, "bake_reduce", fake_bake)
    monkeypatch.setattr(worker.heavy, "_HEARTBEAT_SECONDS", 0.05)
    model = _generated_model(client, runner, make_image_project)
    textured = _scene(_textured_sphere(4, _texture())).export(file_type="glb")
    _save(client, model["id"], model["current_version"]["id"], textured)

    job = client.post(
        f"/api/v1/models/{model['id']}/enhance", json={"quality_mode": "professional"}
    ).json()
    assert worker.heavy._heavy_local_work.acquire(blocking=False)  # another heavy job
    threading.Timer(0.5, worker.heavy.release).start()
    runner.run_until_idle()
    final = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert final["status"] == "succeeded", final["error"]
    assert baked == [True]
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    assert any("before baking" in e["message"] for e in events)
    assert worker.heavy._heavy_local_work.acquire(blocking=False)  # released again
    worker.heavy.release()
