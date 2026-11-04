"""Photogrammetry on a real photo capture, through the whole application.

Runs only when AI3D_REAL_CAPTURE_DIR points at a folder of photos of one
object (and COLMAP 4 is configured). Datasets are not part of the repository;
see docs/TESTING.md for the one this was verified with (AliceVision's Buddha,
CC BY 4.0). With AI3D_REAL_CAPTURE_KEEP set, the reconstructed and cleaned
models are copied there for inspection.

    AI3D_REAL_CAPTURE_DIR=… AI3D_COLMAP_PATH=… uv run pytest backend/tests/test_real_captures.py -s
"""

import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai3d.workers import JobRunner
from ai3d_engine.runtime import find_tool

CAPTURE = os.environ.get("AI3D_REAL_CAPTURE_DIR")
KEEP = os.environ.get("AI3D_REAL_CAPTURE_KEEP")
PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not CAPTURE, reason="set AI3D_REAL_CAPTURE_DIR to a real photo capture"),
]


def _photos() -> list[Path]:
    assert CAPTURE is not None
    return sorted(p for p in Path(CAPTURE).iterdir() if p.suffix.lower() in PHOTO_TYPES)


def _keep(client: TestClient, version: dict[str, Any], name: str) -> None:
    if KEEP:
        Path(KEEP).mkdir(parents=True, exist_ok=True)
        (Path(KEEP) / name).write_bytes(client.get(version["file_url"]).content)


def test_a_real_capture_becomes_a_measured_textured_model(
    client: TestClient, runner: JobRunner, make_project: Any
) -> None:
    if find_tool("colmap") is None:
        pytest.skip("set AI3D_COLMAP_PATH to COLMAP 4")
    photos = _photos()
    assert len(photos) >= 20, "a real capture set needs 20+ photos"
    project = make_project(input_mode="multi_view", default_quality_mode="fast")
    for start in range(0, len(photos), 10):
        batch = [
            ("files", (p.name, p.read_bytes(), PHOTO_TYPES[p.suffix.lower()]))
            for p in photos[start : start + 10]
        ]
        response = client.post(f"/api/v1/projects/{project['id']}/images", files=batch)
        assert response.status_code == 200, response.text
        assert all(r["status"] == "created" for r in response.json()["results"])

    # Overlap measured with real feature matching before reconstructing.
    started = time.monotonic()
    overlap = client.post(f"/api/v1/projects/{project['id']}/capture-assessment/overlap")
    assert overlap.status_code == 200, overlap.text
    check = next(c for c in overlap.json()["checks"] if c["code"] == "overlap")
    print(f"\noverlap: {check['status']}: {check['message']} ({time.monotonic() - started:.0f} s)")
    assert check["status"] in {"pass", "warning"}, check

    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={
            "inputs": [{"image_id": i["id"]} for i in _images(client, project["id"])],
            "provider": "local-photogrammetry",
            "processing_mode": "local",
        },
    )
    assert job.status_code == 201, job.text
    started = time.monotonic()
    runner.run_until_idle()
    seconds = time.monotonic() - started
    result = client.get(f"/api/v1/jobs/{job.json()['id']}").json()
    assert result["status"] == "succeeded", result.get("error")

    model = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"][0]
    version = model["current_version"]
    metrics = version["quality_report"]["metrics"]
    recon = metrics.get("reconstruction") or version["stats"].get("reconstruction") or {}
    print(
        f"reconstruction: {seconds:.0f} s, {recon.get('registered_images')} of "
        f"{len(photos)} photos placed, {recon.get('sparse_points')} points, "
        f"{metrics.get('triangle_count')} triangles, "
        f"reprojection error {recon.get('mean_reprojection_error_px')} px, "
        f"dense={recon.get('dense')}, textured={recon.get('textured')}"
    )
    report = version["quality_report"]
    print(
        "report:",
        report["status"],
        [i["code"] for i in report["issues"] if i["severity"] != "info"],
    )
    assert version["surface_provenance"] == "measured"
    assert recon.get("registered_images", 0) >= 0.8 * len(photos)
    assert version["quality_report"]["status"] != "fail", version["quality_report"]
    _keep(client, version, "reconstructed.glb")

    # Clean-up for a scan: remove the table it stood on.
    enhance = client.post(
        f"/api/v1/models/{model['id']}/enhance",
        json={"quality_mode": "high_quality", "remove_ground": True},
    )
    assert enhance.status_code == 201, enhance.text
    runner.run_until_idle()
    enhance_job = client.get(f"/api/v1/jobs/{enhance.json()['id']}").json()
    assert enhance_job["status"] == "succeeded", enhance_job.get("error")
    cleaned = client.get(f"/api/v1/models/{model['id']}").json()["current_version"]
    if cleaned["version_number"] == 2:
        enhancement = cleaned["quality_report"]["metrics"].get("enhancement", [])
        print(f"enhance steps: { ({s['code']: s['status'] for s in enhancement}) }")
        _keep(client, cleaned, "cleaned.glb")
    else:  # nothing to change (e.g. no ground plane found): no new version, by design
        events = client.get(f"/api/v1/jobs/{enhance_job['id']}/events").json()["items"]
        print("enhance: no changes:", events[-1]["message"])

    formats = client.get("/api/v1/exports/formats").json()["items"]
    for fmt in (f["format"] for f in formats if f["available"]):
        export = client.post(
            f"/api/v1/model-versions/{cleaned['id']}/exports", json={"format": fmt}
        ).json()
        assert export["status"] == "ready", (fmt, export.get("error_message"))
        assert export["validation"]["textured"] is (fmt not in {"stl", "3mf"}), fmt


def _images(client: TestClient, project_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/api/v1/projects/{project_id}/images").json()["items"])
