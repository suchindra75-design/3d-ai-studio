"""The GPU server's API with the stand-in backend (no GPU): auth, jobs, files, limits."""

import io
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from ai3d_gpu.app import create_app
from ai3d_gpu.backends.base import CancelledError, Generated, GenerationSettings, ViewPhoto
from ai3d_gpu.backends.stand_in import StandInBackend, StandInImage, StandInText
from ai3d_gpu.config import ConfigError, ServerConfig
from ai3d_gpu.framing import FRAME_RATIO, frame

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _photo(
    width: int = 400, height: int = 600, colour: tuple[int, int, int] = (200, 40, 40)
) -> bytes:
    """A red ellipse (200 × 400 px) on a plain white background, as JPEG."""
    image = Image.new("RGB", (width, height), (245, 245, 245))
    ImageDraw.Draw(image).ellipse((100, 100, 300, 500), fill=colour)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _client(
    tmp_path: Path, backend: Any = None, *, scenes: bool = True, **overrides: Any
) -> TestClient:
    config = ServerConfig(token=TOKEN, backend="stand-in", work_dir=tmp_path / "work", **overrides)
    extra = {"text": StandInText(), "image": StandInImage()} if scenes else {}
    return TestClient(create_app(config, backend or StandInBackend(), **extra))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _client(tmp_path) as c:
        yield c


def _wait(client: TestClient, job_id: str) -> dict[str, Any]:
    for _ in range(200):
        job = client.get(f"/v1/jobs/{job_id}", headers=AUTH).json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return dict(job)
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _submit(client: TestClient, photos: list[bytes], roles: str = "", **settings: Any) -> Any:
    files = [("photos", (f"p{i}.jpg", data, "image/jpeg")) for i, data in enumerate(photos)]
    return client.post(
        "/v1/jobs",
        headers=AUTH,
        files=files,
        data={"roles": roles, "settings": json.dumps(settings)},
    )


def test_the_server_refuses_to_start_without_a_strong_token() -> None:
    with pytest.raises(ConfigError, match="AI3D_GPU_TOKEN"):
        ServerConfig.from_env({"AI3D_GPU_TOKEN": "short"})
    with pytest.raises(ConfigError):
        ServerConfig.from_env({})
    assert ServerConfig.from_env({"AI3D_GPU_TOKEN": TOKEN}).token == TOKEN


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]
)
def test_every_request_needs_the_token(client: TestClient, headers: dict[str, str]) -> None:
    assert client.get("/v1/health", headers=headers).status_code == 401
    assert _submit_without_auth(client, headers).status_code == 401
    assert client.get("/v1/jobs/abc", headers=headers).status_code == 401


def _submit_without_auth(client: TestClient, headers: dict[str, str]) -> Any:
    return client.post(
        "/v1/jobs", headers=headers, files=[("photos", ("p.jpg", _photo(), "image/jpeg"))]
    )


def test_health_names_the_backend_and_the_queue(client: TestClient) -> None:
    health = client.get("/v1/health", headers=AUTH).json()
    assert health["ok"] is True
    assert health["backend"] == "stand-in"
    assert health["queued"] == 0
    assert health["kinds"] == ["model", "text", "image"]
    assert health["text_model"] == "stand-in text (no AI model)"


def test_one_photo_becomes_a_coloured_model_and_a_framed_cutout(client: TestClient) -> None:
    response = _submit(client, [_photo()], seed=3)
    assert response.status_code == 202, response.text
    job = _wait(client, response.json()["id"])
    assert job["status"] == "succeeded", job
    assert set(job["files"]) == {"model.glb", "cutout-source.png"}

    glb = client.get(f"/v1/jobs/{job['id']}/files/model.glb", headers=AUTH)
    mesh = trimesh.load(io.BytesIO(glb.content), file_type="glb", force="mesh")
    assert len(mesh.faces) > 1000
    colour = mesh.visual.vertex_colors[:, :3].mean(axis=0)
    assert colour[0] > 2 * colour[1]  # red, stored linear as glTF requires

    cutout = Image.open(
        io.BytesIO(
            client.get(f"/v1/jobs/{job['id']}/files/cutout-source.png", headers=AUTH).content
        )
    )
    assert cutout.mode == "RGBA"
    alpha = np.asarray(cutout.getchannel("A"))
    assert alpha[0, 0] == 0
    assert alpha[cutout.height // 2, cutout.width // 2] == 255
    result = client.get(f"/v1/jobs/{job['id']}/files/result.json", headers=AUTH).json()
    # The ellipse is 400 px tall; the square is 1.2 × that, in the photo's pixels.
    (view,) = result["views"]
    assert view["crop_side"] == pytest.approx(400 * FRAME_RATIO, abs=3)
    assert result["seed"] == 3


def test_four_views_are_framed_one_by_one(client: TestClient) -> None:
    photos = [_photo(), _photo(colour=(40, 40, 200)), _photo(), _photo()]
    job = _wait(client, _submit(client, photos, "front,left,back,right").json()["id"])
    assert job["status"] == "succeeded"
    assert {f for f in job["files"] if f.startswith("cutout-")} == {
        "cutout-front.png",
        "cutout-left.png",
        "cutout-back.png",
        "cutout-right.png",
    }


@pytest.mark.parametrize(
    ("photos", "roles", "settings", "status"),
    [
        (5, "", {}, 422),  # too many
        (2, "left,front", {}, 422),  # the first must be the front
        (2, "front,front", {}, 422),  # each role once
        (2, "front", {}, 422),  # one role per photo
        (1, "top", {}, 422),
        (1, "", {"structure_steps": 1000}, 422),
    ],
)
def test_bad_requests_are_refused(
    client: TestClient, photos: int, roles: str, settings: dict[str, Any], status: int
) -> None:
    assert _submit(client, [_photo()] * photos, roles, **settings).status_code == status


def test_only_images_within_the_size_limit_are_accepted(tmp_path: Path) -> None:
    with _client(tmp_path, max_upload_bytes=50_000) as client:
        too_big = _photo(2000, 3000)
        assert len(too_big) > 50_000
        assert _submit(client, [too_big]).status_code == 413
        not_image = client.post(
            "/v1/jobs", headers=AUTH, files=[("photos", ("x.jpg", b"not an image", "image/jpeg"))]
        )
        assert not_image.status_code == 422


def test_files_are_only_the_ones_the_job_made(client: TestClient) -> None:
    job = _wait(client, _submit(client, [_photo()]).json()["id"])
    for name in ("input-0-source", "..%2F..%2Fetc%2Fpasswd", "model.obj"):
        assert client.get(f"/v1/jobs/{job['id']}/files/{name}", headers=AUTH).status_code == 404


def test_a_finished_job_can_be_deleted(client: TestClient, tmp_path: Path) -> None:
    job = _wait(client, _submit(client, [_photo()]).json()["id"])
    folder = tmp_path / "work" / job["id"]
    assert folder.is_dir()
    assert client.delete(f"/v1/jobs/{job['id']}", headers=AUTH).status_code == 204
    assert not folder.exists()
    assert client.get(f"/v1/jobs/{job['id']}", headers=AUTH).status_code == 404


class _SlowBackend:
    """Waits until released or cancelled, so queueing and cancelling can be watched."""

    name, model, device = "slow", "slow", "cpu"

    def __init__(self) -> None:
        self.release = threading.Event()

    def generate(
        self,
        views: list[ViewPhoto],
        settings: GenerationSettings,
        progress: Callable[[str, float], None],
        cancelled: Callable[[], bool],
    ) -> Generated:
        progress("generating", 0.1)
        while not self.release.wait(0.02):
            if cancelled():
                raise CancelledError
        return StandInBackend().generate(views, settings, progress, cancelled)


def test_jobs_queue_on_the_one_gpu_and_can_be_cancelled(tmp_path: Path) -> None:
    backend = _SlowBackend()
    with _client(tmp_path, backend=backend, max_queue=2) as client:
        first = _submit(client, [_photo()]).json()["id"]
        second = _submit(client, [_photo()]).json()["id"]
        assert _submit(client, [_photo()]).status_code == 503  # queue full
        for _ in range(100):
            if client.get(f"/v1/jobs/{first}", headers=AUTH).json()["status"] == "running":
                break
            time.sleep(0.02)
        assert client.get(f"/v1/jobs/{second}", headers=AUTH).json()["status"] == "queued"
        assert (
            client.post(f"/v1/jobs/{second}/cancel", headers=AUTH).json()["status"] == "cancelled"
        )
        assert client.delete(f"/v1/jobs/{first}", headers=AUTH).status_code == 409  # still running
        client.post(f"/v1/jobs/{first}/cancel", headers=AUTH)
        assert _wait(client, first)["status"] == "cancelled"


def test_framing_matches_trellis() -> None:
    """A 100 × 300 px object: TRELLIS measures it as 299 px (last row minus first)
    and frames a square of int(1.2 × 299) = 358 px around its middle."""
    rgba = np.zeros((800, 600, 4), np.uint8)
    rgba[200:500, 250:350] = (255, 0, 0, 255)
    framed = frame(Image.fromarray(rgba), scale=0.5)  # the photo was shrunk to half
    assert framed.side == int(299 * FRAME_RATIO) / 0.5  # in the original photo's pixels
    assert framed.model_input.size == (518, 518)
    middle = np.asarray(framed.model_input)[259, 259]
    assert tuple(middle) == (255, 0, 0)
    assert tuple(np.asarray(framed.model_input)[5, 5]) == (0, 0, 0)  # on black, as TRELLIS


def test_a_text_job_returns_the_models_reply(client: TestClient) -> None:
    response = client.post(
        "/v1/texts",
        headers=AUTH,
        json={
            "system": "Answer with a JSON scene plan.",
            "prompt": "A red car and a palm tree, with a dog",
            "settings": {"seed": 5, "max_tokens": 500},
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["kind"] == "text"
    job = _wait(client, response.json()["id"])
    assert job["status"] == "succeeded", job
    assert job["files"] == ["text.json"]
    reply = client.get(f"/v1/jobs/{job['id']}/files/text.json", headers=AUTH).json()["text"]
    plan = json.loads(reply)
    assert [o["name"] for o in plan["objects"]] == ["red car", "palm tree", "dog"]
    result = client.get(f"/v1/jobs/{job['id']}/files/result.json", headers=AUTH).json()
    assert result["model"] == "stand-in text (no AI model)"
    assert client.delete(f"/v1/jobs/{job['id']}", headers=AUTH).status_code == 204


def test_an_image_job_paints_one_picture_of_the_requested_size(client: TestClient) -> None:
    response = client.post(
        "/v1/images",
        headers=AUTH,
        json={"prompt": "a red car", "settings": {"seed": 2, "width": 512, "height": 384}},
    )
    assert response.status_code == 202, response.text
    job = _wait(client, response.json()["id"])
    assert job["status"] == "succeeded", job
    assert job["files"] == ["image.png"]
    data = client.get(f"/v1/jobs/{job['id']}/files/image.png", headers=AUTH).content
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (512, 384)
    assert client.get(f"/v1/jobs/{job['id']}/files/model.glb", headers=AUTH).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"prompt": ""},
        {"prompt": "x", "settings": {"width": 500}},  # not a multiple of 16
        {"prompt": "x", "settings": {"steps": 0}},
        {"prompt": "x" * 2001},
    ],
)
def test_bad_image_requests_are_refused(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/v1/images", headers=AUTH, json=body).status_code == 422


def test_text_and_image_jobs_need_the_token(client: TestClient) -> None:
    assert client.post("/v1/texts", json={"prompt": "x"}).status_code == 401
    assert client.post("/v1/images", json={"prompt": "x"}).status_code == 401


def test_without_scene_models_only_3d_jobs_are_offered(tmp_path: Path) -> None:
    with _client(tmp_path, scenes=False) as client:
        assert client.get("/v1/health", headers=AUTH).json()["kinds"] == ["model"]
        response = client.post("/v1/texts", headers=AUTH, json={"prompt": "a car"})
        assert response.status_code == 501
        assert "AI3D_GPU_SCENES" in response.json()["detail"]


class _Parking(StandInBackend):
    parked = 0

    def park(self) -> None:
        self.parked += 1


def test_other_models_leave_the_gpu_before_a_job_runs(tmp_path: Path) -> None:
    shape = _Parking()
    with _client(tmp_path, backend=shape) as client:
        text = client.post("/v1/texts", headers=AUTH, json={"prompt": "a car"}).json()
        assert _wait(client, text["id"])["status"] == "succeeded"
        assert shape.parked == 1
        model = _submit(client, [_photo()]).json()
        assert _wait(client, model["id"])["status"] == "succeeded"
        assert shape.parked == 1  # its own job: it stays


def test_scene_settings_come_from_the_environment() -> None:
    env = {"AI3D_GPU_TOKEN": TOKEN}
    config = ServerConfig.from_env(env)
    assert config.scenes is True
    assert config.text_model_id == "Qwen/Qwen3-4B-Instruct-2507"
    assert config.image_model_id == "black-forest-labs/FLUX.1-schnell"
    assert config.text_model_revision
    assert config.image_model_revision
    assert ServerConfig.from_env({**env, "AI3D_GPU_SCENES": "off"}).scenes is False
