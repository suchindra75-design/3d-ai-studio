"""CGI scenes from a prompt (D76), end to end: your GPU server (the real server code
with its stand-in models, over real HTTP) plans the scene, paints and shapes each
object; this computer refines the objects and Blender (real, when installed) renders
the shot."""

import io
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import trimesh
import uvicorn
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.workers.runner import JobRunner
from ai3d_engine.blender import BlenderFailedError, check_blender
from ai3d_gpu.app import create_app as create_gpu_app
from ai3d_gpu.backends.base import TextRequest
from ai3d_gpu.backends.stand_in import StandInBackend, StandInImage, StandInText
from ai3d_gpu.config import ServerConfig
from tests.conftest import stored_keys

KEY = "gpu-server"
TOKEN = "s" * 40
BLENDER = check_blender() is None
needs_blender = pytest.mark.skipif(not BLENDER, reason="needs Blender 4.2+ to render")

# Small and quick, but the same path as the real profiles.
TEST_RENDER = {
    "width": 320,
    "height": 180,
    "fps": 12,
    "engine": "eevee",
    "samples": 4,
    "still_engine": "eevee",
    "still_samples": 8,
    "motion_blur": False,
    "raytracing": False,
}


class _Counting(StandInImage):
    painted = 0

    def paint(self, *args: Any, **kwargs: Any) -> Image.Image:
        type(self).painted += 1
        return super().paint(*args, **kwargs)


def _serve(tmp_path: Path, text: Any) -> Iterator[tuple[str, ServerConfig]]:
    config = ServerConfig(token=TOKEN, backend="stand-in", work_dir=tmp_path / "gpu-work")
    app = create_gpu_app(config, StandInBackend(), text=text, image=_Counting())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", config
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def gpu_server(tmp_path: Path) -> Iterator[tuple[str, ServerConfig]]:
    _Counting.painted = 0
    yield from _serve(tmp_path, StandInText())


@pytest.fixture
def small_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai3d.models.enums import QualityMode  # noqa: PLC0415
    from ai3d.workers import scenes  # noqa: PLC0415

    monkeypatch.setattr(scenes, "RENDER_PRESETS", dict.fromkeys(QualityMode, TEST_RENDER))


def _configure(client: TestClient, url: str) -> None:
    for path, body in (("base-url", {"base_url": url}), ("api-key", {"api_key": TOKEN})):
        assert client.put(f"/api/v1/providers/{KEY}/{path}", json=body).status_code == 200


def _scene(client: TestClient, project_id: str, **body: Any) -> Any:
    payload = {
        "prompt": "A red car and a palm tree",
        "seconds": 2,
        "quality_mode": "fast",
        "send_to_gpu_server": True,
    } | body
    return client.post(f"/api/v1/projects/{project_id}/scenes", json=payload)


def test_scenes_need_consent_and_your_gpu_server(
    client: TestClient, make_project: Callable[..., dict[str, Any]]
) -> None:
    project = make_project(input_mode="prompt")
    readiness = client.get("/api/v1/scenes/readiness").json()
    assert readiness["ready"] is False
    assert any("GPU server" in p for p in readiness["problems"])

    refused = _scene(client, project["id"], send_to_gpu_server=False)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "consent_required"

    missing = _scene(client, project["id"])
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "gpu_server_not_configured"
    assert client.get(f"/api/v1/projects/{project['id']}/scenes").json()["items"] == []


@pytest.mark.parametrize("prompt", ["", "ab", "x" * 2001])
def test_a_prompt_must_say_something(
    client: TestClient, make_project: Callable[..., dict[str, Any]], prompt: str
) -> None:
    project = make_project(input_mode="prompt")
    assert _scene(client, project["id"], prompt=prompt).status_code == 422


def test_blender_is_needed_to_render(
    client: TestClient,
    make_project: Callable[..., dict[str, Any]],
    gpu_server: tuple[str, ServerConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai3d.services import scenes  # noqa: PLC0415

    monkeypatch.setattr(scenes, "check_blender", lambda: "Blender was not found.")
    _configure(client, gpu_server[0])
    project = make_project(input_mode="prompt")
    response = _scene(client, project["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "blender_missing"
    readiness = client.get("/api/v1/scenes/readiness").json()
    assert readiness == {"ready": False, "problems": [readiness["problems"][0]]}
    assert "Blender" in readiness["problems"][0]


def _models(client: TestClient, project_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/api/v1/projects/{project_id}/models").json()["items"])


@pytest.mark.slow
@needs_blender
@pytest.mark.usefixtures("small_renders")
def test_a_prompt_becomes_a_rendered_scene(
    client: TestClient,
    runner: JobRunner,
    make_project: Callable[..., dict[str, Any]],
    gpu_server: tuple[str, ServerConfig],
) -> None:
    url, config = gpu_server
    _configure(client, url)
    assert client.get("/api/v1/scenes/readiness").json() == {"ready": True, "problems": []}
    project = make_project(input_mode="prompt")

    created = _scene(client, project["id"], shot="orbit", lighting="daylight")
    assert created.status_code == 201, created.text
    scene = created.json()
    assert scene["status"] == "making"
    assert scene["job"]["kind"] == "scene"
    assert scene["job"]["provider"]["key"] == KEY
    assert any("FLUX.1-schnell" in line for line in scene["job"]["provider_summary"])

    runner.run_until_idle()
    scene = client.get(f"/api/v1/scenes/{scene['id']}").json()
    assert scene["status"] == "ready", scene["job"]
    assert [o["name"] for o in scene["objects"]] == ["red car", "palm tree"]
    assert scene["shot"] == "orbit"
    assert scene["lighting"] == "daylight"  # the user's choice over the planner's
    assert all(o["model_id"] and o["position_m"] for o in scene["objects"])
    assert _Counting.painted == 2  # one reference picture per object

    # Each object is a model of the project, named, refined (textured) on this computer.
    models = {m["name"]: m for m in _models(client, project["id"])}
    assert set(models) == {"red car", "palm tree"}
    data = client.get(models["red car"]["current_version"]["file_url"]).content
    mesh = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="glb", force="mesh")
    assert isinstance(mesh.visual, trimesh.visual.TextureVisuals)

    events = client.get(f"/api/v1/jobs/{scene['job']['id']}/events").json()["items"]
    assert any(e["message"].startswith("Plan: 2 objects (red car, palm tree)") for e in events)
    assert events[-1]["message"].startswith("Your scene is ready")

    files = {f["name"]: f for f in scene["files"]}
    assert set(files) == {"video", "still", "blend", "glb"}
    assert scene["render"]["frames"] == 24  # 2 s at 12 frames per second
    video = client.get(files["video"]["url"])
    assert video.headers["content-type"] == "video/mp4"
    assert video.content[4:8] == b"ftyp"
    part = client.get(files["video"]["url"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206  # seeking in the player works
    still = Image.open(io.BytesIO(client.get(files["still"]["url"]).content))
    assert still.size == (320, 180)
    download = client.get(files["blend"]["url"], params={"download": True})
    assert "attachment" in download.headers["content-disposition"]
    # A compressed .blend file (Zstandard) or a plain one.
    assert download.content[:4] == b"\x28\xb5\x2f\xfd" or download.content[:7] == b"BLENDER"
    shot = trimesh.load(
        trimesh.util.wrap_as_stream(client.get(files["glb"]["url"]).content), file_type="glb"
    )
    assert len(shot.geometry) >= 2  # the objects (and the ground)

    # Nothing is left on the GPU server.
    assert not any(config.work_dir.iterdir())

    # Render again: other shot and light, same objects, nothing sent to the server.
    again = client.post(
        f"/api/v1/scenes/{scene['id']}/render",
        json={"shot": "static", "lighting": "night", "seconds": 2},
    )
    assert again.status_code == 201, again.text
    again_id = again.json()["id"]
    assert again.json()["job"]["processing_mode"] == "local"
    runner.run_until_idle()
    second = client.get(f"/api/v1/scenes/{again_id}").json()
    assert second["status"] == "ready", second["job"]
    assert (second["shot"], second["lighting"]) == ("static", "night")
    assert second["source_scene_id"] == scene["id"]
    assert [o["model_id"] for o in second["objects"]] == [o["model_id"] for o in scene["objects"]]
    assert _Counting.painted == 2
    listed = client.get(f"/api/v1/projects/{project['id']}/scenes").json()["items"]
    assert [s["id"] for s in listed] == [again_id, scene["id"]]

    # Deleting a scene removes its files; its objects stay as models.
    prefix = f"projects/{project['id']}/scenes/{scene['id']}/"
    assert stored_keys(client, prefix)
    assert client.delete(f"/api/v1/scenes/{scene['id']}").status_code == 204
    assert client.get(f"/api/v1/scenes/{scene['id']}").status_code == 404
    assert stored_keys(client, prefix) == []
    assert len(_models(client, project["id"])) == 2


@pytest.mark.slow
@needs_blender
@pytest.mark.usefixtures("small_renders")
def test_a_retry_keeps_the_objects_already_made(
    client: TestClient,
    runner: JobRunner,
    make_project: Callable[..., dict[str, Any]],
    gpu_server: tuple[str, ServerConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai3d.workers import scenes  # noqa: PLC0415

    real = scenes.run_blender
    calls = []

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise BlenderFailedError("render_scene", "out of memory", [])
        return real(*args, **kwargs)

    monkeypatch.setattr(scenes, "run_blender", fail_once)
    _configure(client, gpu_server[0])
    project = make_project(input_mode="prompt")
    scene = _scene(client, project["id"], prompt="A robot").json()
    runner.run_until_idle()
    failed = client.get(f"/api/v1/scenes/{scene['id']}").json()
    assert failed["status"] == "failed"
    assert failed["job"]["error"]["code"] == "render_failed"
    assert "out of memory" in failed["job"]["error"]["message"]
    assert len(_models(client, project["id"])) == 1  # the robot was made

    retried = client.post(f"/api/v1/jobs/{failed['job']['id']}/retry", json={})
    assert retried.status_code == 201, retried.text
    runner.run_until_idle()
    ready = client.get(f"/api/v1/scenes/{scene['id']}").json()
    assert ready["status"] == "ready", ready["job"]
    assert ready["job"]["id"] == retried.json()["id"]
    assert len(_models(client, project["id"])) == 1  # not made again
    assert _Counting.painted == 1


class _Confused(StandInText):
    def write(self, request: TextRequest, cancelled: Callable[[], bool]) -> str:
        return "I am not sure what you mean."


def test_an_unreadable_plan_is_asked_for_again_then_fails(
    client: TestClient,
    runner: JobRunner,
    make_project: Callable[..., dict[str, Any]],
    tmp_path: Path,
) -> None:
    for url, _config in _serve(tmp_path, _Confused()):
        _configure(client, url)
        project = make_project(input_mode="prompt")
        response = _scene(client, project["id"])
        if response.status_code == 409:  # no Blender here: checked before anything is sent
            assert not BLENDER
            return
        runner.run_until_idle()
        scene = client.get(f"/api/v1/scenes/{response.json()['id']}").json()
        assert scene["status"] == "failed"
        assert scene["job"]["error"]["code"] == "plan_unreadable"
        events = client.get(f"/api/v1/jobs/{scene['job']['id']}/events").json()["items"]
        assert sum("asking again" in e["message"] for e in events) == 2
        assert _models(client, project["id"]) == []
