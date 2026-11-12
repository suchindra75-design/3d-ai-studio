"""Scene job: a prompt becomes a rendered CGI shot.

planning → for each object: painting → shaping → refining → saving → arranging →
rendering → saving the files.

On the user's GPU server (``gpu-server/``, nothing else is contacted): a language
model writes the scene plan (``processing/scene_plan.py``), FLUX.1-schnell paints a
reference picture of each object and TRELLIS shapes it. On this computer: each
object is cleaned up and textured like any GPU-server model and saved as a model of
the project (so it can be inspected, edited and exported on its own); then the
objects are laid out from their real sizes and Blender builds the set, the light,
the camera move and the motions and renders a still, an MP4, a .blend file and an
animated GLB.

Progress is kept in the scene's plan after every object, so a job that is
interrupted or retried continues with the objects still missing. A "render again"
job (``render_only``) skips straight to arranging and rendering.
"""

import hashlib
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import trimesh
from sqlalchemy.orm import selectinload

from ai3d.models import GenerationJob, Model3D, ModelVersion, Scene
from ai3d.models.enums import AssetKind, JobEventType, JobKind, JobStatus
from ai3d.processing.mesh import validate_mesh_file
from ai3d.processing.scene_plan import (
    OBJECT_PRESETS,
    PLANNER_INSTRUCTIONS,
    RENDER_PRESETS,
    Footprint,
    PlanError,
    ScenePlan,
    arrange,
    object_image_prompt,
    parse_plan,
    with_choices,
)
from ai3d.providers.base import (
    GenerationRequest,
    ProviderError,
    ProviderErrorCode,
    ProviderInput,
    TaskStatus,
)
from ai3d.providers.gpu_server import GpuServerProvider
from ai3d.services.models import save_generated_model, version_file
from ai3d.storage.keys import project_prefix
from ai3d.workers import heavy
from ai3d.workers.generation import _refined
from ai3d.workers.runner import JobContext
from ai3d_engine.blender import (
    BlenderCancelledError,
    BlenderFailedError,
    BlenderNotFoundError,
    run_blender,
)

log = logging.getLogger(__name__)

PLAN_ATTEMPTS = 3
MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_POLL_FAILURES = 10
PLAN_SHARE = (0.0, 0.08)
OBJECTS_SHARE = (0.08, 0.78)
RENDER_SHARE = (0.82, 0.98)
FILES = {
    "video": ("mp4", "video/mp4"),
    "still": ("png", "image/png"),
    "blend": ("blend", "application/x-blender"),
    "glb": ("glb", "model/gltf-binary"),
}


class _FailedError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _CancelledError(Exception):
    pass


def scene_key(project_id: uuid.UUID, scene_id: uuid.UUID, filename: str) -> str:
    return f"{project_prefix(project_id)}scenes/{scene_id}/{filename}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "scene"


# -- loading and saving -------------------------------------------------------------------


def _load(ctx: JobContext) -> tuple[GenerationJob, Scene]:
    with ctx.session() as db:
        job = db.get(GenerationJob, ctx.job_id, options=[selectinload(GenerationJob.inputs)])
        if job is None:
            raise _FailedError("job_missing", "The job no longer exists.")
        scene_id = job.params.get("scene_id")
        scene = db.get(Scene, uuid.UUID(scene_id)) if scene_id else None
        if scene is None:
            raise _FailedError("scene_missing", "The scene was deleted.")
        db.expunge(job)
        db.expunge(scene)
        return job, scene


def _save_scene(ctx: JobContext, scene_id: uuid.UUID, **values: Any) -> None:
    with ctx.session() as db:
        scene = db.get(Scene, scene_id)
        if scene is None:
            raise _FailedError("scene_missing", "The scene was deleted while it was being made.")
        for name, value in values.items():
            setattr(scene, name, value)
        db.commit()


def _stored_plan(scene: Scene) -> ScenePlan | None:
    return ScenePlan.model_validate(scene.plan) if scene.plan.get("objects") else None


# -- the GPU server -----------------------------------------------------------------------


def _provider(ctx: JobContext) -> GpuServerProvider:
    with ctx.session() as db:
        provider, _credential = ctx.runner.providers.instantiate(db, GpuServerProvider.key)
    assert isinstance(provider, GpuServerProvider)
    return provider


def _wait(
    ctx: JobContext, provider: GpuServerProvider, task_id: str, span: tuple[float, float]
) -> TaskStatus:
    interval = ctx.settings.provider_poll_interval
    deadline = time.monotonic() + ctx.settings.job_timeout_seconds
    failures = 0
    reported = -1.0
    while True:
        if ctx.heartbeat():
            try:
                provider.cancel(task_id)
            finally:
                provider.forget(task_id)
            raise _CancelledError
        try:
            status = provider.poll(task_id)
            failures = 0
        except ProviderError as exc:
            failures += 1
            if not exc.retryable or failures > MAX_POLL_FAILURES:
                raise
            ctx.sleep(min(30.0, interval * 2**failures))
            continue
        if status.state == "succeeded":
            return status
        if status.state == "failed":
            provider.forget(task_id)
            raise status.error or ProviderError(
                ProviderErrorCode.TASK_FAILED, "The GPU server reported a failure."
            )
        if status.state == "cancelled":
            raise _CancelledError
        progress = span[0] + (span[1] - span[0]) * (status.progress or 0.0)
        if progress - reported >= 0.01:
            ctx.update(progress=progress)
            reported = progress
        if time.monotonic() > deadline:
            raise ProviderError(
                ProviderErrorCode.TIMEOUT,
                f"The GPU server did not finish within "
                f"{ctx.settings.job_timeout_seconds // 60} minutes.",
                retryable=False,
            )
        ctx.sleep(interval)


def _plan(
    ctx: JobContext,
    provider: GpuServerProvider,
    scene: Scene,
    workdir: Path,
) -> ScenePlan:
    ctx.stage("planning", "Planning the scene on your GPU server.", PLAN_SHARE[0])
    seed = int(hashlib.sha256(scene.prompt.encode()).hexdigest()[:6], 16)
    for attempt in range(PLAN_ATTEMPTS):
        task = provider.submit_text(
            PLANNER_INSTRUCTIONS,
            scene.prompt,
            {"seed": seed + attempt, "max_tokens": 1500, "temperature": 0.7 - 0.2 * attempt},
        )
        _wait(ctx, provider, task, PLAN_SHARE)
        reply_path = workdir / f"plan-{attempt}.json"
        provider.fetch_file(task, "text.json", reply_path, MAX_TEXT_BYTES)
        provider.forget(task)
        try:
            text = str(json.loads(reply_path.read_text(encoding="utf-8")).get("text", ""))
            plan = parse_plan(text, prompt=scene.prompt)
        except (PlanError, ValueError) as exc:
            if attempt < PLAN_ATTEMPTS - 1:
                ctx.event(
                    JobEventType.WARNING,
                    f"The scene plan could not be read ({exc}); asking again.",
                    stage="planning",
                )
            continue
        return with_choices(plan, scene.settings)
    raise _FailedError(
        "plan_unreadable",
        f"The scene planner gave no usable plan in {PLAN_ATTEMPTS} tries. "
        "Try describing the objects more plainly.",
    )


def _make_object(
    ctx: JobContext,
    provider: GpuServerProvider,
    job: GenerationJob,
    plan: ScenePlan,
    *,
    index: int,
    workdir: Path,
) -> ModelVersion:
    item = plan.objects[index]
    count = len(plan.objects)
    low, high = OBJECTS_SHARE
    step = (high - low) / count
    start = low + step * index
    preset = OBJECT_PRESETS[job.quality_mode]
    seed = int(hashlib.sha256(f"{item.name}|{item.description}".encode()).hexdigest()[:6], 16)
    folder = workdir / f"object-{index}"
    folder.mkdir(parents=True, exist_ok=True)

    ctx.stage(
        "painting",
        f"Painting a reference picture of the {item.name} ({index + 1} of {count}).",
        start,
    )
    size = preset["image_size"]
    task = provider.submit_image(
        object_image_prompt(item, plan.style),
        {"seed": seed, "width": size, "height": size, "steps": preset["image_steps"]},
    )
    _wait(ctx, provider, task, (start, start + step * 0.3))
    picture = folder / "reference.png"
    provider.fetch_file(task, "image.png", picture, MAX_IMAGE_BYTES)
    provider.forget(task)

    ctx.stage("shaping", f"Shaping the {item.name} in 3D with TRELLIS.", start + step * 0.3)
    steps = preset["shape_steps"]
    request = GenerationRequest(
        job_id=job.id,
        kind=JobKind.IMAGE_TO_3D,
        quality_mode=job.quality_mode,
        inputs=(
            ProviderInput(
                path=picture,
                mime_type="image/png",
                role="source",
                sha256=hashlib.sha256(picture.read_bytes()).hexdigest(),
            ),
        ),
        settings={"generation": {"structure_steps": steps, "detail_steps": steps, "seed": seed}},
    )
    task = provider.submit(request)
    status = _wait(ctx, provider, task, (start + step * 0.3, start + step * 0.75))
    result = provider.download_result(task, status, folder)
    result = replace(result, preview_path=picture)  # the picture is its thumbnail
    result = _refined(ctx, job, result)

    report = validate_mesh_file(result.mesh_path, file_type=result.mesh_format)
    if not report.usable:
        reason = "; ".join(i.message for i in report.issues if i.severity == "error")
        raise _FailedError("invalid_output", f"The {item.name} is not a usable model: {reason}")
    ctx.stage("saving", f"Saving the {item.name} as a model of the project.", start + step * 0.95)
    with ctx.session() as db:
        version = save_generated_model(
            db,
            ctx.runner.storage,
            ctx.settings,
            job=job,
            provider=GpuServerProvider,
            result=result,
            report=report,
        )
        model = db.get(Model3D, version.model_id)
        if model is not None:
            model.name = item.name[:200]
        db.commit()
        db.refresh(version)
        db.expunge(version)
    ctx.event(
        JobEventType.LOG,
        f"Made the {item.name} ({report.metrics.get('triangle_count', 0):,} triangles).",
        stage="saving",
        data={"model_version_id": str(version.id)},
    )
    return version


def _make_objects(
    ctx: JobContext, job: GenerationJob, scene: Scene, plan: ScenePlan | None, workdir: Path
) -> ScenePlan:
    provider = _provider(ctx)
    try:
        if plan is None:
            plan = _plan(ctx, provider, scene, workdir)
            _save_scene(ctx, scene.id, plan=plan.model_dump(mode="json"), title=plan.title)
            names = ", ".join(o.name for o in plan.objects)
            ctx.event(
                JobEventType.LOG,
                f"Plan: {len(plan.objects)} object{'s' if len(plan.objects) != 1 else ''} "
                f"({names}); {plan.lighting.replace('_', ' ')} light, "
                f"{plan.ground} ground, {plan.shot.replace('_', ' ')} shot, {plan.style} look.",
                stage="planning",
                data={"plan": plan.model_dump(mode="json")},
            )
        for index, item in enumerate(plan.objects):
            if item.model_version_id and _version_exists(ctx, item.model_version_id):
                continue
            version = _make_object(ctx, provider, job, plan, index=index, workdir=workdir)
            plan.objects[index] = item.model_copy(
                update={
                    "model_id": str(version.model_id),
                    "model_version_id": str(version.id),
                }
            )
            _save_scene(ctx, scene.id, plan=plan.model_dump(mode="json"))  # a checkpoint
    finally:
        provider.close()
    return plan


def _version_exists(ctx: JobContext, version_id: str) -> bool:
    with ctx.session() as db:
        return db.get(ModelVersion, uuid.UUID(version_id)) is not None


# -- rendering ----------------------------------------------------------------------------


def _footprint(path: Path, size_m: float) -> Footprint:
    mesh = trimesh.load(path, force="mesh")
    extents = mesh.extents if isinstance(mesh, trimesh.Trimesh) else (1.0, 1.0, 1.0)
    scale = size_m / max(float(max(extents)), 1e-6)
    # glTF axes: x across, y up, z towards the viewer.
    return Footprint(
        width=float(extents[0]) * scale,
        depth=float(extents[2]) * scale,
        height=float(extents[1]) * scale,
    )


def _render_fraction(path: Path) -> float:
    """How far Blender is, from the progress file its script writes after each frame."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stage = str(data.get("stage"))
        done, total = int(data.get("done", 0)), max(1, int(data.get("total", 1)))
    except (ValueError, OSError):
        return 0.0
    if stage == "building":
        return 0.0
    if stage == "still":
        return 0.05
    if stage == "saving":
        return 1.0
    return 0.05 + 0.93 * min(1.0, done / total)


def _progress_reader(ctx: JobContext, path: Path) -> Any:
    """Blender's cancel check, which also reports its progress on the job."""
    low, high = RENDER_SHARE
    state = {"reported": -1.0, "at": 0.0}

    def check() -> bool:
        now = time.monotonic()
        if now - state["at"] >= 2.0 and path.is_file():
            state["at"] = now
            progress = low + (high - low) * _render_fraction(path)
            if progress - state["reported"] >= 0.01:
                ctx.update(progress=progress)
                state["reported"] = progress
        return ctx.heartbeat()

    return check


def _arranged(ctx: JobContext, plan: ScenePlan, folder: Path) -> tuple[ScenePlan, list[Path]]:
    """Each object's model copied here, and the plan with ground positions that keep
    the objects apart (from their real footprints)."""
    paths: list[Path] = []
    footprints: list[Footprint] = []
    for index, item in enumerate(plan.objects):
        if not item.model_version_id:
            raise _FailedError("object_missing", f"The {item.name} was never made.")
        path = folder / f"object-{index}.glb"
        with ctx.session() as db:
            try:
                _version, asset = version_file(db, uuid.UUID(item.model_version_id), AssetKind.MESH)
            except Exception as exc:  # the model or version was deleted meanwhile
                raise _FailedError(
                    "object_missing",
                    f"The model of the {item.name} was deleted; make the scene again.",
                ) from exc
            key = asset.storage_key
        with ctx.runner.storage.open(key) as src, path.open("wb") as out:
            shutil.copyfileobj(src, out)
        paths.append(path)
        footprints.append(_footprint(path, item.size_m))
    positions = arrange(plan.objects, footprints)
    objects = [
        o.model_copy(update={"position_m": p}) for o, p in zip(plan.objects, positions, strict=True)
    ]
    return plan.model_copy(update={"objects": objects}), paths


def _blender_request(
    plan: ScenePlan, paths: list[Path], render: dict[str, Any], output: dict[str, str]
) -> dict[str, Any]:
    return {
        "objects": [
            {
                "name": item.name,
                "path": str(path),
                "size_m": item.size_m,
                "position": list(item.position_m or (0.0, 0.0)),
                "elevation_m": item.elevation_m,
                "facing_deg": item.facing_deg,
                "motion": item.motion,
            }
            for item, path in zip(plan.objects, paths, strict=True)
        ],
        "environment": {
            "lighting": plan.lighting,
            "ground": plan.ground,
            "ground_colour": list(plan.ground_colour) if plan.ground_colour else None,
            "sky_colour": list(plan.sky_colour) if plan.sky_colour else None,
        },
        "camera": {
            "shot": plan.shot,
            "angle": plan.angle,
            "lens_mm": plan.lens_mm,
            "focus": plan.focus,
        },
        "style": plan.style,
        "render": render,
        "output": output,
    }


def _run_blender(
    ctx: JobContext, request: dict[str, Any], folder: Path, frames: int
) -> dict[str, Any]:
    if not heavy.wait_turn(ctx, "Waiting for another local job to finish before rendering."):
        raise _CancelledError
    try:
        return run_blender(
            "render_scene",
            request,
            workdir=folder,
            timeout=min(6 * 3600.0, 900.0 + frames * 90.0),
            cancel=_progress_reader(ctx, Path(request["output"]["progress"])),
            poll_seconds=0.5,
        )
    except BlenderCancelledError:
        raise _CancelledError from None
    except BlenderNotFoundError as exc:
        raise _FailedError(
            "blender_missing", "Rendering needs Blender 4.2 or newer on this computer."
        ) from exc
    except BlenderFailedError as exc:
        raise _FailedError(
            "render_failed", f"Blender could not render the scene: {exc.message}"
        ) from exc
    finally:
        heavy.release()


def _stored_files(
    ctx: JobContext, scene: Scene, output: dict[str, str], names: dict[str, str]
) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for key, (_ext, mime) in FILES.items():
        path = Path(output[key])
        if not path.is_file():
            continue
        storage_key = scene_key(scene.project_id, scene.id, names[key])
        ctx.runner.storage.save_file(storage_key, path)
        files[key] = {
            "key": storage_key,
            "filename": names[key],
            "mime_type": mime,
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    if "video" not in files:
        raise _FailedError("render_failed", "Blender wrote no video.")
    return files


def _render(
    ctx: JobContext, job: GenerationJob, scene: Scene, plan: ScenePlan, workdir: Path
) -> None:
    ctx.stage("arranging", "Setting the stage from the objects' real sizes.", 0.79)
    folder = workdir / "render"
    folder.mkdir(parents=True, exist_ok=True)
    plan, paths = _arranged(ctx, plan, folder)

    seconds = float(scene.settings.get("seconds") or 6.0)
    preset = dict(RENDER_PRESETS[job.quality_mode]) | {"seconds": seconds}
    names = {key: f"{_slug(plan.title)}.{ext}" for key, (ext, _mime) in FILES.items()}
    output = {key: str(folder / name) for key, name in names.items()}
    output["progress"] = str(folder / "progress.json")
    size = f"{preset['width']}×{preset['height']}"
    ctx.stage(
        "rendering",
        f"Rendering {seconds:g} s at {size} with Blender on this computer.",
        RENDER_SHARE[0],
    )
    result = _run_blender(
        ctx,
        _blender_request(plan, paths, preset, output),
        folder,
        round(seconds * preset["fps"]),
    )

    ctx.stage("saving", "Saving the video, the still and the scene files.", 0.985)
    files = _stored_files(ctx, scene, output, names)
    render = {
        k: result[k]
        for k in ("frames", "fps", "width", "height", "engines", "seconds")
        if k in result
    }
    _save_scene(ctx, scene.id, plan=plan.model_dump(mode="json"), files=files, render=render)
    engines = ", ".join(e.upper() for e in sorted(set(render.get("engines", {}).values())))
    if ctx.finish(JobStatus.SUCCEEDED, stage="done", progress=1.0, cancel_requested=False):
        ctx.event(
            JobEventType.STATUS,
            f"Your scene is ready: {seconds:g} s at {size} ({engines}), with a still image, "
            "a Blender file and an animated GLB.",
            stage="done",
            progress=1.0,
            data={"scene_id": str(scene.id)},
        )


# -- the job ------------------------------------------------------------------------------


def _run(ctx: JobContext, workdir: Path) -> None:
    job, scene = _load(ctx)
    plan = _stored_plan(scene)
    if job.params.get("render_only"):
        if plan is None:
            raise _FailedError("plan_missing", "The scene has no plan to render.")
    else:
        plan = _make_objects(ctx, job, scene, plan, workdir)
    if ctx.heartbeat():
        raise _CancelledError
    _render(ctx, job, scene, plan, workdir)


def run_scene(ctx: JobContext) -> None:
    tmp_root = ctx.settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="scene-", dir=tmp_root))
    try:
        _run(ctx, workdir)
    except _CancelledError:
        if ctx.finish(JobStatus.CANCELLED, stage=None):
            ctx.event(
                JobEventType.STATUS,
                "Cancelled. Objects already made are kept as models of the project.",
            )
    except _FailedError as exc:
        if ctx.finish(JobStatus.FAILED, error_code=exc.code, error_message=exc.message):
            ctx.event(JobEventType.ERROR, exc.message, data={"code": exc.code})
        log.info("scene failed", extra={"job_id": str(ctx.job_id), "code": exc.code})
    except ProviderError as exc:
        if ctx.finish(JobStatus.FAILED, error_code=exc.code.value, error_message=exc.message):
            ctx.event(JobEventType.ERROR, exc.message, data={"code": exc.code.value})
        log.info("scene failed", extra={"job_id": str(ctx.job_id), "code": exc.code.value})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
