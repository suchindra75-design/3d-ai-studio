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

import cv2
import numpy as np
import pytest
import trimesh
from fastapi.testclient import TestClient
from PIL import Image

from ai3d.core.config import Settings
from ai3d.processing import geometry, refine
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
        # The cut-out the engine saves: the sphere's outline from TripoSR's camera.
        cutout = workdir / "cutout.png"
        outline = np.zeros((256, 256), np.uint8)
        pixels = refine._pinhole(np.asarray(sphere.vertices), 256)
        for triangle in pixels[sphere.faces]:
            cv2.fillPoly(outline, [np.round(triangle).astype(np.int32)], 255)
        colour = np.full((256, 256, 3), (230, 120, 20), np.uint8)
        Image.fromarray(np.dstack([colour, outline])).save(cutout)
        return {
            "ok": True,
            "device": "cpu",
            "background_removed": True,
            "triangles": len(sphere.faces),
            "timings": {"generating": 1.0, "meshing": 2.0},
            "seconds_wall": 3.0,
            "glb": str(glb),
            "cutout_image": str(cutout),
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
    assert plan["provider_settings"] == {
        "resolution": 256,
        "refine": {"triangles": 40_000, "texture_size": 2048},
    }

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
    assert stages[-4:] == ["meshing", "refining", "validating", "saving"]
    assert any("Nothing was uploaded" in (e["message"] or "") for e in events)
    (refined,) = [e for e in events if (e["message"] or "").startswith("Refined in")]
    assert "colours baked into a 1024×1024 px texture" in refined["message"]
    assert "the photo was projected onto" in refined["message"]

    (call,) = installed
    assert call["resolution"] == 160  # the project's default quality (fast)
    assert call["bytes"][:2] == b"\xff\xd8"  # a metadata-free JPEG copy of the photo
    assert b"Exif" not in call["bytes"][:4096]

    model = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"][0]
    version = model["current_version"]
    assert version["provider_key"] == LOCAL
    assert version["source"] == "generated"
    assert version["surface_provenance"] == "inferred"
    data = client.get(version["file_url"]).content
    assert data[:4] == b"glTF"
    mesh = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="glb", force="mesh")
    assert isinstance(mesh.visual, trimesh.visual.TextureVisuals)
    texture = np.asarray(mesh.visual.material.baseColorTexture.convert("RGB"))
    assert texture.shape == (1024, 1024, 3)  # the fast profile's texture size
    # The front shows the cut-out's orange; the back keeps the model's own green
    # (linear 40, 160, 60 is sRGB about 110, 209, 136).
    uv = mesh.visual.uv
    texel = np.stack([uv[:, 0] * 1023, (1 - uv[:, 1]) * 1023], axis=1).round().astype(int)
    colours = texture[texel[:, 1], texel[:, 0]].astype(float)
    front = mesh.vertices[:, 2] > 0.4
    back = mesh.vertices[:, 2] < -0.3
    assert np.abs(colours[front].mean(axis=0) - (230, 120, 20)).max() < 20
    assert np.abs(colours[back].mean(axis=0) - (110, 209, 136)).max() < 10


def test_a_failed_refinement_keeps_the_model_as_generated(
    client: TestClient,
    runner: JobRunner,
    make_image_project: Any,
    installed: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("no charts")

    monkeypatch.setattr(worker, "refine", broken)
    project, ids = make_image_project(1)
    job = client.post(
        f"/api/v1/projects/{project['id']}/generations",
        json={"inputs": [{"image_id": ids[0]}], "processing_mode": "local"},
    ).json()
    runner.run_until_idle()
    job = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert job["status"] == "succeeded", job
    events = client.get(f"/api/v1/jobs/{job['id']}/events").json()["items"]
    assert any(
        e["message"]
        == "Refining the model did not work (ValueError: no charts); it is kept as TripoSR made it."
        for e in events
    )
    version = client.get(f"/api/v1/projects/{project['id']}/models").json()["items"][0][
        "current_version"
    ]
    data = client.get(version["file_url"]).content
    mesh = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="glb", force="mesh")
    assert mesh.visual.kind == "vertex"  # TripoSR's own per-vertex colours


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
    assert 30_000 < len(mesh.faces) <= 42_000  # refined to about 40,000 (high quality)
    assert isinstance(mesh.visual, trimesh.visual.TextureVisuals)
    texture = np.asarray(mesh.visual.material.baseColorTexture.convert("RGB"))
    side = texture.shape[0]
    assert texture.shape == (2048, 2048, 3)
    texel = np.stack(
        [mesh.visual.uv[:, 0] * (side - 1), (1 - mesh.visual.uv[:, 1]) * (side - 1)], axis=1
    )
    texel = texel.round().astype(int)
    colours = texture[texel[:, 1], texel[:, 0]].astype(float)  # sRGB at each vertex
    assert colours.std() > 10  # real colours, not one flat value
    extents = mesh.extents
    assert extents[1] == max(extents)  # upright: the plush toy is taller than wide or deep
    assert "NORMAL" in data[:4096].decode("latin-1")  # smooth normals for shading
    # Not mirrored or inside out, and the colours belong where they are (Phase 11 found
    # that the marching-cubes stand-in returned swapped axes): this plush has orange
    # feet with yellow soles, and dark eyes on the front of its head (+Z, glTF's
    # front). Warm = orange or yellow (the projected photo shows the yellow soles,
    # which TripoSR's own colours made orange); the green body and white belly are not.
    assert mesh.volume > 0
    assert report["metrics"]["inside_out"] is False
    height = (mesh.vertices[:, 1] - mesh.vertices[:, 1].min()) / np.ptp(mesh.vertices[:, 1])
    depth = (mesh.vertices[:, 2] - mesh.vertices[:, 2].min()) / np.ptp(mesh.vertices[:, 2])
    warm = (
        (colours[:, 0] > 120)
        & (colours[:, 1] <= colours[:, 0])
        & (colours[:, 2] < 0.5 * colours[:, 0])
    )
    assert warm[height < 0.15].mean() > 0.2
    assert warm[height > 0.5].mean() < 0.01
    # The pupils are dark grey in the photo (its darkest pixels are about 50), and the
    # front now shows the photo's own colours.
    eyes = (colours.max(axis=1) < 60) & (height > 0.6)
    assert eyes.sum() > 10
    assert depth[eyes].mean() > 0.6

    # Every available export of this real AI model re-opens with its colours: as the
    # texture, or (3MF, which has no textures) as per-vertex colours; STL has neither.
    formats = client.get("/api/v1/exports/formats").json()["items"]
    for fmt in (f for f in formats if f["available"]):
        export = client.post(
            f"/api/v1/model-versions/{version['id']}/exports", json={"format": fmt["format"]}
        ).json()
        assert export["status"] == "ready", (fmt["format"], export.get("error_message"))
        found = export["validation"]
        if fmt["format"] == "stl":
            assert not found["textured"]
            assert not found["coloured"]
        elif fmt["format"] == "3mf":
            assert found["coloured"] is True
        else:
            assert found["textured"] is True, fmt["format"]
    print(f"\nreal TripoSR run: {seconds:.0f} s, {len(mesh.faces):,} triangles")


# -- AI texture upscaling (Real-ESRGAN compact) ------------------------------------


def _textured_box(size: int) -> trimesh.Scene:
    box = trimesh.creation.box()
    box.unmerge_vertices()
    rng = np.random.default_rng(2)
    box.visual = trimesh.visual.TextureVisuals(
        uv=rng.random((len(box.vertices), 2)),
        image=Image.fromarray(rng.integers(0, 255, (size, size, 3), dtype=np.uint8)),
    )
    return trimesh.Scene(box)


def _stand_in_upscaler(calls: list[dict[str, Any]]) -> Any:
    def upscale(source: Path, output: Path, workdir: Path, **kw: Any) -> dict[str, Any]:
        calls.append({"source": source, **kw})
        image = Image.open(source)
        side = min(image.width * kw["scale"], kw["max_size"])
        image.resize((side, side)).save(output)
        return {"ok": True}

    return upscale


def test_upscaling_enlarges_textures_within_the_size_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(geometry.local_ai_engine, "upscaler_problem", lambda: None)
    monkeypatch.setattr(geometry.local_ai_engine, "upscale", _stand_in_upscaler(calls))
    scene = _textured_box(64)
    step = geometry.upscale_textures(scene, 4, 4096, workdir=tmp_path)
    assert step.status == "applied"
    assert "64×64 → 256×256" in step.detail
    assert "does not recover detail" in step.detail
    (mesh,) = scene.geometry.values()
    assert mesh.visual.material.image.size == (256, 256)

    big = _textured_box(256)
    assert geometry.upscale_textures(big, 2, 256, workdir=tmp_path).status == "skipped"
    plain = trimesh.Scene(trimesh.creation.box())
    step = geometry.upscale_textures(plain, 2, 4096, workdir=tmp_path)
    assert step.status == "skipped"
    assert step.detail == "No texture to upscale."
    assert len(calls) == 1


def test_upscaling_is_unavailable_until_the_upscaler_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "engine_dir", lambda: tmp_path / "no-engine")
    step = geometry.upscale_textures(_textured_box(64), 2, 4096, workdir=tmp_path)
    assert step.status == "unavailable"
    assert "not installed" in step.detail


@pytest.mark.slow
@pytest.mark.skipif(
    not REAL_ENGINE or engine.upscaler_problem(Path(REAL_ENGINE)) is not None,
    reason="set AI3D_LOCAL_AI_DIR to an engine with the upscaler (install-upscaler)",
)
def test_real_upscaler_makes_a_real_photo_four_times_larger_and_sharper(tmp_path: Path) -> None:
    photo = _real_photo()
    assert photo is not None
    original = Image.open(photo).convert("RGB")
    # The most detailed 128-px patch (eyes, seams): a flat patch says nothing.
    grey = np.asarray(original.convert("L"), float)
    patches = [
        (float(np.abs(np.diff(grey[y : y + 128, x : x + 128], axis=0)).mean()), x, y)
        for y in range(0, original.height - 128, 64)
        for x in range(0, original.width - 128, 64)
    ]
    _, x, y = max(patches)
    crop = original.crop((x, y, x + 128, y + 128))
    source = tmp_path / "crop.png"
    crop.save(source)
    engine.configure(Path(REAL_ENGINE))
    result = engine.upscale(source, tmp_path / "big.png", tmp_path / "run", scale=4, max_size=4096)
    assert (result["width"], result["height"]) == (512, 512)
    ai = Image.open(tmp_path / "big.png").convert("L")
    lanczos = crop.resize((512, 512), Image.Resampling.LANCZOS).convert("L")

    def edges(image: Image.Image) -> float:
        a = np.asarray(image, float)
        return float(np.abs(np.diff(a, axis=0)).mean() + np.abs(np.diff(a, axis=1)).mean())

    assert edges(ai) > edges(lanczos) * 1.1  # sharper than plain resizing


def test_upscaling_respects_the_input_limit_free_memory_and_tile_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(geometry.local_ai_engine, "upscaler_problem", lambda: None)
    monkeypatch.setattr(geometry.local_ai_engine, "upscale", _stand_in_upscaler(calls))

    # Over the configured input limit: kept, and said why.
    limits = geometry.UpscaleLimits(max_input_px=100)
    step = geometry.upscale_textures(_textured_box(128), 2, 4096, workdir=tmp_path, limits=limits)
    assert step.status == "skipped"
    assert "over 100 px (128×128)" in step.detail

    # Not enough free memory for this texture: kept, with the numbers.
    need = geometry.upscale_memory_bytes(128, 128, 256, 256)
    limits = geometry.UpscaleLimits(available_bytes=need - 1)
    step = geometry.upscale_textures(_textured_box(128), 2, 4096, workdir=tmp_path, limits=limits)
    assert step.status == "skipped"
    assert "Not enough free memory (128×128 needs about" in step.detail
    assert calls == []

    # Enough memory: upscaled with the configured tile size.
    limits = geometry.UpscaleLimits(tile_px=64, available_bytes=need)
    step = geometry.upscale_textures(_textured_box(128), 2, 4096, workdir=tmp_path, limits=limits)
    assert step.status == "applied"
    assert [c["tile"] for c in calls] == [64]
    # The estimate grows with the output: a 4096 px result needs about 100 MB more for
    # the output alone than a 512 px one.
    small = geometry.upscale_memory_bytes(256, 256, 512, 512)
    assert geometry.upscale_memory_bytes(1024, 1024, 4096, 4096) > small + 100 * 1024**2


def test_upscale_limits_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI3D_UPSCALE_MAX_INPUT_PX", "2048")
    monkeypatch.setenv("AI3D_UPSCALE_TILE_PX", "128")
    settings = Settings()
    assert (settings.upscale_max_input_px, settings.upscale_tile_px) == (2048, 128)
    monkeypatch.setenv("AI3D_UPSCALE_TILE_PX", "8")  # too small to be useful
    with pytest.raises(ValueError, match="upscale_tile_px"):
        Settings()


@pytest.mark.slow
@pytest.mark.skipif(
    not REAL_ENGINE or engine.upscaler_problem(Path(REAL_ENGINE)) is not None,
    reason="set AI3D_LOCAL_AI_DIR to an engine with the upscaler (install-upscaler)",
)
def test_real_upscaler_gives_the_same_image_with_small_tiles(tmp_path: Path) -> None:
    """Small tiles (less memory) must not leave seams: same result as one big tile."""
    photo = _real_photo()
    assert photo is not None
    crop = Image.open(photo).convert("RGB").resize((160, 160))
    source = tmp_path / "crop.png"
    crop.save(source)
    engine.configure(Path(REAL_ENGINE))
    out = {}
    for tile in (32, 256):
        engine.upscale(
            source,
            tmp_path / f"t{tile}.png",
            tmp_path / f"run{tile}",
            scale=2,
            max_size=4096,
            tile=tile,
        )
        out[tile] = np.asarray(Image.open(tmp_path / f"t{tile}.png").convert("RGB"), float)
    assert out[32].shape == out[256].shape == (320, 320, 3)
    diff = np.abs(out[32] - out[256])
    print(f"\ntile 32 vs 256: mean {diff.mean():.3f}, 99.9th pct {np.percentile(diff, 99.9):.1f}")
    assert diff.mean() < 0.5
    assert np.percentile(diff, 99.9) < 8
