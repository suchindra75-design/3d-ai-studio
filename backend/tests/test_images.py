import hashlib
import io
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.core.config import Settings
from ai3d.main import create_app
from ai3d.services.images import sanitize_filename
from tests import images
from tests.conftest import make_settings, stored_keys

MakeProject = Callable[..., dict[str, Any]]
Upload = Callable[..., Any]


def _storage_files(client: TestClient) -> list[str]:
    return stored_keys(client)


@pytest.mark.parametrize(
    ("fmt", "mime", "ext"),
    [("JPEG", "image/jpeg", "jpg"), ("PNG", "image/png", "png"), ("WEBP", "image/webp", "webp")],
)
def test_upload_supported_formats(
    client: TestClient,
    make_project: MakeProject,
    upload: Upload,
    settings: Settings,
    fmt: str,
    mime: str,
    ext: str,
) -> None:
    pid = make_project()["id"]
    data = images.encode(images.textured(), fmt)
    result = upload(pid, (f"photo.{ext}", data, "application/octet-stream"))
    assert result["created"] == 1
    image = result["results"][0]["image"]
    assert image["mime_type"] == mime  # detected from content, not the declared type
    assert image["width"] == 800
    assert image["height"] == 600
    assert image["sha256"] == hashlib.sha256(data).hexdigest()
    assert f"original.{ext}" in " ".join(_storage_files(client))


def test_original_bytes_are_preserved_and_served(
    client: TestClient, make_project: MakeProject, upload: Upload, sharp_jpeg: bytes
) -> None:
    pid = make_project()["id"]
    image = upload(pid, ("a.jpg", sharp_jpeg, "image/jpeg"))["results"][0]["image"]

    original = client.get(image["file_url"])
    assert original.status_code == 200
    assert original.content == sharp_jpeg
    assert original.headers["content-type"] == "image/jpeg"
    assert "immutable" in original.headers["cache-control"]

    thumb = client.get(image["thumbnail_url"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(thumb.content)) as t:
        assert max(t.size) == 512


def test_extension_and_declared_type_are_ignored(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project()["id"]
    result = upload(pid, ("notes.jpg", b"definitely not an image", "image/jpeg"))
    assert result["rejected"] == 1
    assert result["results"][0]["error"]["code"] == "unsupported_media_type"


def test_unsupported_real_image_format(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project()["id"]
    gif = images.encode(images.textured(64, 64), "GIF")
    result = upload(pid, ("a.gif", gif, "image/gif"))
    assert result["results"][0]["error"]["code"] == "unsupported_media_type"
    assert result["results"][0]["error"]["details"]["detected_format"] == "GIF"


def test_truncated_image_is_rejected(
    client: TestClient, make_project: MakeProject, upload: Upload, sharp_jpeg: bytes
) -> None:
    pid = make_project()["id"]
    result = upload(pid, ("cut.jpg", sharp_jpeg[: len(sharp_jpeg) // 2], "image/jpeg"))
    assert result["results"][0]["error"]["code"] == "corrupt_image"


def test_empty_file_is_rejected(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project()["id"]
    result = upload(pid, ("empty.jpg", b"", "image/jpeg"))
    assert result["results"][0]["error"]["code"] == "empty_file"


def test_one_bad_file_does_not_fail_batch(
    client: TestClient,
    make_project: MakeProject,
    upload: Upload,
    sharp_jpeg: bytes,
    settings: Settings,
) -> None:
    pid = make_project()["id"]
    result = upload(
        pid,
        ("good.jpg", sharp_jpeg, "image/jpeg"),
        ("bad.jpg", b"garbage", "image/jpeg"),
    )
    assert (result["created"], result["rejected"]) == (1, 1)
    # Rejected files leave nothing behind (original + thumbnail of the good one only).
    assert len(_storage_files(client)) == 2
    assert not list((settings.data_dir / "tmp").glob("*/*"))


def test_duplicate_upload_is_idempotent(
    client: TestClient, make_project: MakeProject, upload: Upload, sharp_jpeg: bytes
) -> None:
    pid = make_project()["id"]
    first = upload(pid, ("a.jpg", sharp_jpeg, "image/jpeg"))["results"][0]
    second = upload(pid, ("renamed.jpg", sharp_jpeg, "image/jpeg"))["results"][0]
    assert second["status"] == "duplicate"
    assert second["image"]["id"] == first["image"]["id"]
    assert client.get(f"/api/v1/projects/{pid}/images").json()["total"] == 1

    # Same bytes in another project are a separate image.
    other = make_project()["id"]
    assert upload(other, ("a.jpg", sharp_jpeg, "image/jpeg"))["created"] == 1


def test_size_limit(tmp_path: Any, sharp_jpeg: bytes) -> None:
    settings = make_settings(tmp_path, None, max_upload_bytes=1024)
    with TestClient(create_app(settings)) as client:
        pid = client.post(
            "/api/v1/projects", json={"name": "p", "input_mode": "single_image"}
        ).json()["id"]
        body = client.post(
            f"/api/v1/projects/{pid}/images", files=[("files", ("a.jpg", sharp_jpeg, "image/jpeg"))]
        ).json()
    assert body["results"][0]["error"]["code"] == "payload_too_large"


def test_pixel_limit(tmp_path: Any, sharp_jpeg: bytes) -> None:
    settings = make_settings(tmp_path, None, max_image_pixels=1000)
    with TestClient(create_app(settings)) as client:
        pid = client.post(
            "/api/v1/projects", json={"name": "p", "input_mode": "single_image"}
        ).json()["id"]
        body = client.post(
            f"/api/v1/projects/{pid}/images", files=[("files", ("a.jpg", sharp_jpeg, "image/jpeg"))]
        ).json()
    assert body["results"][0]["error"]["code"] == "payload_too_large"


def test_image_count_limit(tmp_path: Any) -> None:
    settings = make_settings(tmp_path, None, max_images_per_project=2)
    with TestClient(create_app(settings)) as client:
        pid = client.post(
            "/api/v1/projects", json={"name": "p", "input_mode": "multi_view"}
        ).json()["id"]
        files = [
            ("files", (f"{i}.png", images.encode(images.textured(seed=i), "PNG"), "image/png"))
            for i in range(3)
        ]
        body = client.post(f"/api/v1/projects/{pid}/images", files=files).json()
    assert [r["status"] for r in body["results"]] == ["created", "created", "rejected"]
    assert body["results"][2]["error"]["code"] == "limit_exceeded"


def test_animated_images_are_rejected(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project()["id"]
    frames = [images.textured(64, 64, seed=i) for i in range(3)]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="WEBP", save_all=True, append_images=frames[1:])
    result = upload(pid, ("anim.webp", buffer.getvalue(), "image/webp"))
    assert result["results"][0]["error"]["code"] == "unsupported_media_type"


def test_exif_metadata_orientation_and_gps_privacy(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project()["id"]
    data = images.jpeg_with_exif(images.textured(800, 600), orientation=6, gps=True)
    image = upload(pid, ("exif.jpg", data, "image/jpeg"))["results"][0]["image"]
    assert image["camera_make"] == "TestCam"
    assert image["camera_model"] == "X100"
    assert image["focal_length_mm"] == pytest.approx(23.0)
    assert image["exif"]["date_time_original"] == "2026:10:01 12:00:00"
    # Orientation 6 = rotated 90°: displayed size is swapped.
    assert (image["width"], image["height"]) == (600, 800)
    assert image["exif_orientation"] == 6
    # GPS is detected (privacy notice) but never exposed.
    assert "gps" not in str(image["exif"]).lower()
    assert "gps_metadata" in {w["code"] for w in image["analysis"]["warnings"]}
    # The original still contains every byte the user uploaded.
    assert client.get(image["file_url"]).content == data
    with Image.open(io.BytesIO(client.get(image["thumbnail_url"]).content)) as t:
        assert t.size[0] < t.size[1]  # thumbnail is upright


def test_filenames_are_sanitised(
    client: TestClient,
    make_project: MakeProject,
    upload: Upload,
    sharp_jpeg: bytes,
    settings: Settings,
) -> None:
    pid = make_project()["id"]
    image = upload(pid, ("../../etc/passwd.jpg", sharp_jpeg, "image/jpeg"))["results"][0]["image"]
    assert image["original_filename"] == "passwd.jpg"
    assert all(f.startswith(f"projects/{pid}/") for f in _storage_files(client))


def test_list_order_and_project_counts(
    client: TestClient, make_project: MakeProject, upload: Upload
) -> None:
    pid = make_project()["id"]
    datas = [images.encode(images.textured(seed=i), "JPEG") for i in range(3)]
    for i, data in enumerate(datas):
        upload(pid, (f"{i}.jpg", data, "image/jpeg"))
    listing = client.get(f"/api/v1/projects/{pid}/images").json()
    assert [i["original_filename"] for i in listing["items"]] == ["0.jpg", "1.jpg", "2.jpg"]
    assert [i["position"] for i in listing["items"]] == [1, 2, 3]

    project = client.get(f"/api/v1/projects/{pid}").json()
    assert project["image_count"] == 3
    assert project["total_image_bytes"] == sum(len(d) for d in datas)
    assert project["cover_image_id"] == listing["items"][0]["id"]


def test_delete_image_removes_files(
    client: TestClient,
    make_project: MakeProject,
    upload: Upload,
    sharp_jpeg: bytes,
    settings: Settings,
) -> None:
    pid = make_project()["id"]
    image = upload(pid, ("a.jpg", sharp_jpeg, "image/jpeg"))["results"][0]["image"]
    assert client.delete(f"/api/v1/images/{image['id']}").status_code == 204
    assert client.get(f"/api/v1/images/{image['id']}").status_code == 404
    assert client.get(image["file_url"]).status_code == 404
    assert _storage_files(client) == []


def test_delete_project_removes_images_and_files(
    client: TestClient,
    make_project: MakeProject,
    upload: Upload,
    sharp_jpeg: bytes,
    settings: Settings,
) -> None:
    keep = make_project()["id"]
    upload(keep, ("keep.jpg", sharp_jpeg, "image/jpeg"))
    pid = make_project()["id"]
    image = upload(pid, ("a.jpg", sharp_jpeg, "image/jpeg"))["results"][0]["image"]
    assert client.delete(f"/api/v1/projects/{pid}").status_code == 204
    assert client.get(f"/api/v1/images/{image['id']}").status_code == 404
    files = _storage_files(client)
    assert files
    assert all(f.startswith(f"projects/{keep}/") for f in files)


def test_upload_to_missing_project(client: TestClient, sharp_jpeg: bytes) -> None:
    response = client.post(
        "/api/v1/projects/00000000-0000-0000-0000-000000000000/images",
        files=[("files", ("a.jpg", sharp_jpeg, "image/jpeg"))],
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C:\\Users\\me\\IMG_1.JPG", "IMG_1.JPG"),
        ("../../etc/pass\x07wd.jpg", "passwd.jpg"),
        ("  spaced name.png  ", "spaced name.png"),
        ("", "image"),
        (None, "image"),
        ("\u202egpj.exe", "gpj.exe"),  # bidi override (category Cf) removed
        ("x" * 300 + ".jpg", "x" * 255),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected
