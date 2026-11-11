"""Local single-image AI generation job: photo → TripoSR → GLB, on this computer.

preparing → background → loading → generating → meshing → refining → validating → saving

Refining (``processing/refine.py``) removes loose fragments, smooths and simplifies
the surface and bakes a texture with the photo projected onto the front. If it
fails, the model is kept as TripoSR made it and the job says so.

With several views (front, left, back, right), TripoSR makes the shape from the
front photo; the others are cut out by the engine too, and refining uses them to
correct the depth and colour the sides they show.

Nothing is uploaded. An interrupted run simply starts again from the photo
(recovery re-queues it); it costs nothing.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from time import monotonic
from typing import Any

from PIL import Image

from ai3d.models import Image as ImageRow
from ai3d.models.enums import JobEventType, JobStatus
from ai3d.processing.mesh import validate_mesh_file
from ai3d.providers.base import ProviderResult
from ai3d.providers.local_ai import LocalAIProvider
from ai3d.services import resources
from ai3d.services.models import save_generated_model
from ai3d.workers import heavy
from ai3d.workers.generation import JobFailedError, _load, _prepare_inputs
from ai3d.workers.refining import RefineInputs, refine_model
from ai3d.workers.runner import JobContext
from ai3d_engine.local_ai import (
    LocalAICancelledError,
    LocalAIFailedError,
    LocalAINotInstalledError,
    generate,
)

log = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 2.0
# Job progress per engine stage (start, end).
_SPAN: dict[str, tuple[float, float]] = {
    "starting": (0.04, 0.08),
    "background": (0.08, 0.14),
    "loading": (0.14, 0.3),
    "generating": (0.3, 0.6),
    "meshing": (0.6, 0.86),
}
_MESSAGES: dict[str, str] = {
    "starting": "Starting the local AI engine.",
    "background": "Removing the background.",
    "loading": "Loading the TripoSR model on this computer.",
    "generating": "Generating the 3D shape from the photo.",
    "meshing": "Building the surface and its colours.",
}


class _CancelledError(Exception):
    pass


class _Progress:
    def __init__(self, ctx: JobContext) -> None:
        self.ctx = ctx
        self.stage: str | None = None
        self.reported = 0.0

    def __call__(self, stage: str, fraction: float) -> None:
        low, high = _SPAN[stage]
        overall = max(low + (high - low) * min(max(fraction, 0.0), 1.0), self.reported)
        self.reported = overall
        if stage != self.stage:
            self.stage = stage
            self.ctx.stage(stage, _MESSAGES[stage], overall)
        else:
            self.ctx.update(progress=overall)


def _cancel_check(ctx: JobContext, deadline: float) -> Any:
    last = [0.0, False]

    def cancelled() -> bool:
        now = monotonic()
        if now > deadline:
            raise JobFailedError(
                "timeout",
                f"Local generation did not finish within "
                f"{ctx.settings.local_ai_timeout_seconds // 60} minutes.",
            )
        if now - last[0] >= _HEARTBEAT_SECONDS:
            last[0] = now
            last[1] = ctx.heartbeat()  # raises when the runner stops or the lease is lost
        return bool(last[1])

    return cancelled


def _run(ctx: JobContext, workdir: Path) -> None:
    job = _load(ctx)
    if job.cancel_requested:
        raise _CancelledError
    ctx.stage("preparing", "Preparing the photo on this computer.", 0.02)
    photos = _prepare_inputs(ctx, job, LocalAIProvider, workdir)
    # Photo pixels as taken, per working-copy pixel (copies of large photos are smaller).
    scales = _pixel_scales(ctx, job, [p.path for p in photos])
    front = next((i for i, p in enumerate(photos) if p.role in ("front", "source")), 0)
    photo = photos[front]
    others = [(p.role, p.path, scales[i]) for i, p in enumerate(photos) if i != front]
    if not heavy.wait_turn(ctx, "Waiting for another local job on this computer to finish."):
        raise _CancelledError
    # The plan checked the disk; it may have filled up while the job waited.
    disk = resources.not_enough_disk(
        resources.LOCAL_AI, resources.machine(ctx.settings.data_dir), LocalAIProvider.display_name
    )
    if disk:
        heavy.release()
        raise JobFailedError("not_enough_disk", disk)
    try:
        settings = dict(job.params.get("provider_settings") or {})
        deadline = monotonic() + ctx.settings.local_ai_timeout_seconds
        result = generate(
            photo.path,
            workdir / "engine",
            resolution=int(settings.get("resolution", 256)),
            timeout=float(ctx.settings.local_ai_timeout_seconds),
            cancel=_cancel_check(ctx, deadline),
            on_progress=_Progress(ctx),
            other_views=[(role, path) for role, path, _ in others],
        )
    finally:
        heavy.release()
    result["front_pixel_scale"] = scales[front]
    for view in result.get("views", []):
        view["pixel_scale"] = next((s for r, _, s in others if r == view["role"]), 1.0)
    timings = result.get("timings", {})
    ctx.event(
        JobEventType.LOG,
        f"Generated on the {str(result['device']).upper()} in {result['seconds_wall']:.0f} s "
        f"(shape {timings.get('generating', 0):.0f} s, surface {timings.get('meshing', 0):.0f} s)"
        f"{'; background removed' if result.get('background_removed') else ''}. "
        "Nothing was uploaded.",
        stage="meshing",
        data={"local_ai": result},
    )

    glb, refined = _refine(ctx, result, settings.get("refine"))

    ctx.stage("validating", "Checking the 3D file.", 0.94)
    report = validate_mesh_file(glb)
    if not report.usable:
        reason = "; ".join(i.message for i in report.issues if i.severity == "error")
        raise JobFailedError("invalid_output", f"The generated model is not usable: {reason}")

    ctx.stage("saving", "Saving the model.", 0.98)
    with ctx.session() as db:
        version = save_generated_model(
            db,
            ctx.runner.storage,
            ctx.settings,
            job=job,
            provider=LocalAIProvider,
            result=ProviderResult(
                mesh_path=glb,
                mesh_format="glb",
                metadata={"local_ai": result} | ({"refine": refined} if refined else {}),
            ),
            report=report,
        )
    if ctx.finish(
        JobStatus.SUCCEEDED,
        stage="done",
        progress=1.0,
        result_model_version_id=version.id,
        cancel_requested=False,
    ):
        ctx.event(
            JobEventType.STATUS,
            f"Model ready ({report.metrics.get('triangle_count', 0):,} triangles).",
            stage="done",
            progress=1.0,
            data={"model_version_id": str(version.id), "report_status": report.status.value},
        )


def _pixel_scales(ctx: JobContext, job: Any, paths: list[Path]) -> list[float]:
    """Per input: pixels of the photo as taken per pixel of its working copy."""
    scales = []
    with ctx.session() as db:
        for item, path in zip(job.inputs, paths, strict=True):
            source = db.get(ImageRow, item.image_id) if item.image_id else None
            with Image.open(path) as opened:
                copy = max(opened.size)
            original = max(source.width, source.height) if source else copy
            scales.append(original / copy if copy else 1.0)
    return scales


def _refine(
    ctx: JobContext, result: dict[str, Any], options: Any
) -> tuple[Path, dict[str, Any] | None]:
    """TripoSR's model refined with the engine's framed photos (front and others)."""
    views = [
        (
            str(view["role"]),
            Path(view["cutout"]),
            float(view["crop_side"]) * float(view.get("pixel_scale", 1.0)),
        )
        for view in result.get("views", [])
        if view.get("ok")
    ]
    crop_side = result.get("crop_side")
    inputs = RefineInputs(
        source="TripoSR",
        photo=Path(result["cutout_image"]) if result.get("cutout_image") else None,
        photo_crop_side=(
            float(crop_side) * float(result.get("front_pixel_scale", 1.0)) if crop_side else None
        ),
        views=views,
    )
    return refine_model(ctx, Path(result["glb"]), options, inputs, progress=0.87)


def run_local_generation(ctx: JobContext) -> None:
    tmp_root = ctx.settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="local-ai-", dir=tmp_root))
    try:
        _run(ctx, workdir)
    except (_CancelledError, LocalAICancelledError):
        if ctx.finish(JobStatus.CANCELLED, stage=None):
            ctx.event(JobEventType.STATUS, "Cancelled. Local generation was stopped.")
    except LocalAINotInstalledError as exc:
        _fail(ctx, "engine_unavailable", str(exc))
    except LocalAIFailedError as exc:
        _fail(ctx, "engine_failed", exc.message, {"log_tail": exc.tail[-10:]})
    except JobFailedError as exc:
        _fail(ctx, exc.code, exc.message, exc.details)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _fail(ctx: JobContext, code: str, message: str, details: dict[str, Any] | None = None) -> None:
    if ctx.finish(
        JobStatus.FAILED, error_code=code, error_message=message, error_details=details or {}
    ):
        ctx.event(JobEventType.ERROR, message, data={"code": code})
    log.info("local generation failed", extra={"job_id": str(ctx.job_id), "code": code})
