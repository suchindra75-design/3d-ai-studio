"""Local single-image AI generation job: photo → TripoSR → GLB, on this computer.

preparing → background → loading → generating → meshing → validating → saving

Nothing is uploaded. An interrupted run simply starts again from the photo
(recovery re-queues it); it costs nothing.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from time import monotonic
from typing import Any

from ai3d.models.enums import JobEventType, JobStatus
from ai3d.processing.mesh import validate_mesh_file
from ai3d.providers.base import ProviderResult
from ai3d.providers.local_ai import LocalAIProvider
from ai3d.services.models import save_generated_model
from ai3d.workers import heavy
from ai3d.workers.generation import JobFailedError, _load, _prepare_inputs
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
    "meshing": (0.6, 0.92),
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
    (photo,) = _prepare_inputs(ctx, job, LocalAIProvider, workdir)
    if not heavy.wait_turn(ctx, "Waiting for another local job on this computer to finish."):
        raise _CancelledError
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
        )
    finally:
        heavy.release()
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

    ctx.stage("validating", "Checking the 3D file.", 0.94)
    glb = Path(result["glb"])
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
            result=ProviderResult(mesh_path=glb, mesh_format="glb", metadata={"local_ai": result}),
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
