from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests import images, scenes

MakeProject = Callable[..., dict[str, Any]]
Upload = Callable[..., Any]


def _assess(client: TestClient, pid: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/projects/{pid}/capture-assessment")
    assert response.status_code == 200
    return dict(response.json())


def _check(assessment: dict[str, Any], code: str) -> dict[str, Any]:
    return next(c for c in assessment["checks"] if c["code"] == code)


def test_single_image_project(
    client: TestClient, make_project: MakeProject, upload: Upload, sharp_jpeg: bytes
) -> None:
    pid = make_project(input_mode="single_image")["id"]
    assert _assess(client, pid)["readiness"] == "insufficient"

    upload(pid, ("a.jpg", sharp_jpeg, "image/jpeg"))
    assessment = _assess(client, pid)
    assert assessment["readiness"] == "ready"
    assert _check(assessment, "image_quality")["status"] == "pass"
    # The permanent single-image limitation is always stated.
    assert _check(assessment, "unseen_surfaces")["status"] == "warning"


def test_multi_view_counts_and_overlap_not_faked(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project(input_mode="multi_view")["id"]
    upload(pid, ("0.jpg", images.encode(images.textured(seed=0), "JPEG"), "image/jpeg"))
    assessment = _assess(client, pid)
    assert assessment["readiness"] == "insufficient"

    for i in range(1, 4):
        upload(pid, (f"{i}.jpg", images.encode(images.textured(seed=i), "JPEG"), "image/jpeg"))
    assessment = _assess(client, pid)
    assert assessment["readiness"] == "needs_attention"
    assert _check(assessment, "image_count")["status"] == "warning"
    assert _check(assessment, "sharpness")["status"] == "pass"
    assert _check(assessment, "overlap")["status"] == "not_assessed"
    assert _check(assessment, "focal_length")["status"] == "not_assessed"


def test_multi_view_flags_blur_lighting_and_zoom(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project(input_mode="multi_view")["id"]
    upload(
        pid, ("sharp.jpg", images.jpeg_with_exif(images.textured(seed=1), focal=24.0), "image/jpeg")
    )
    blurry = upload(
        pid, ("blur.jpg", images.jpeg_with_exif(images.blurred(seed=2), focal=50.0), "image/jpeg")
    )["results"][0]["image"]
    dark = Image.eval(images.textured(seed=3), lambda v: v // 6)
    upload(pid, ("dark.jpg", images.jpeg_with_exif(dark, focal=24.0), "image/jpeg"))

    assessment = _assess(client, pid)
    sharpness = _check(assessment, "sharpness")
    assert sharpness["status"] in {"warning", "fail"}
    assert blurry["id"] in sharpness["image_ids"]
    assert _check(assessment, "lighting_consistency")["status"] == "warning"
    assert _check(assessment, "focal_length")["status"] == "warning"


def test_assessment_for_missing_project(client: TestClient) -> None:
    response = client.get(
        "/api/v1/projects/00000000-0000-0000-0000-000000000000/capture-assessment"
    )
    assert response.status_code == 404


# -- overlap (real feature matching) ---------------------------------------------------


def _upload_all(client: TestClient, pid: str, files: list[tuple[str, bytes]]) -> list[str]:
    response = client.post(
        f"/api/v1/projects/{pid}/images",
        files=[("files", (name, data, "image/jpeg")) for name, data in files],
    )
    assert response.status_code == 200, response.text
    return [r["image"]["id"] for r in response.json()["results"]]


def _measure(client: TestClient, pid: str) -> dict[str, Any]:
    response = client.post(f"/api/v1/projects/{pid}/capture-assessment/overlap")
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.fixture(scope="module")
def ring() -> list[tuple[str, bytes]]:
    """10 photos walking around one object: neighbours overlap."""
    return scenes.photo_set(scenes.orbit(10, 25.0), width=480, height=360, focal=420.0)


def test_overlap_is_measured_on_request_and_cached(
    client: TestClient, make_project: Callable[..., dict[str, Any]], ring: list[Any]
) -> None:
    pid = make_project(input_mode="multi_view")["id"]
    _upload_all(client, pid, ring)
    before = _check(_assess(client, pid), "overlap")
    assert before["status"] == "not_assessed"
    assert before["action"] == "measure_overlap"

    measured = _check(_measure(client, pid), "overlap")
    assert measured["status"] == "pass", measured
    assert measured["title"] == "Photos overlap well"
    assert "All 10 photos connect into one set" in measured["message"]
    assert measured["action"] is None
    # Cached for this exact photo set.
    assert _check(_assess(client, pid), "overlap") == measured


def test_unrelated_photos_are_named_and_the_cache_goes_stale(
    client: TestClient, make_project: Callable[..., dict[str, Any]], ring: list[Any]
) -> None:
    pid = make_project(input_mode="multi_view")["id"]
    _upload_all(client, pid, ring)
    _measure(client, pid)
    stray = images.encode(images.textured(480, 360, seed=999), "JPEG")
    [stray_id] = _upload_all(client, pid, [("stray.jpg", stray)])
    # The photo set changed: the old measurement no longer applies.
    assert _check(_assess(client, pid), "overlap")["status"] == "not_assessed"

    check = _check(_measure(client, pid), "overlap")
    assert check["status"] == "warning"
    assert "1 photo(s) overlap with no other photo" in check["message"]
    assert "stray.jpg" in check["message"]
    assert check["image_ids"] == [stray_id]


def test_photos_that_share_nothing_fail(
    client: TestClient, make_project: Callable[..., dict[str, Any]]
) -> None:
    pid = make_project(input_mode="multi_view")["id"]
    _upload_all(
        client,
        pid,
        [
            (f"p{i}.jpg", images.encode(images.textured(320, 240, seed=50 + i), "JPEG"))
            for i in range(4)
        ],
    )
    check = _check(_measure(client, pid), "overlap")
    assert check["status"] == "fail"
    assert "cannot connect them" in check["message"]


def test_overlap_needs_two_photos(
    client: TestClient, make_project: Callable[..., dict[str, Any]], sharp_jpeg: bytes
) -> None:
    pid = make_project(input_mode="multi_view")["id"]
    _upload_all(client, pid, [("one.jpg", sharp_jpeg)])
    response = client.post(f"/api/v1/projects/{pid}/capture-assessment/overlap")
    assert response.status_code == 422
