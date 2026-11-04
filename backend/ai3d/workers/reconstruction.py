"""Local photogrammetry job: photos → COLMAP → GLB, entirely on this machine.

preparing → features → matching → mapping → filtering → meshing → texturing
→ validating → saving

Nothing is uploaded. An interrupted reconstruction is simply run again from
the photos (recovery re-queues it); it costs nothing.
"""

import logging
import shutil
import tempfile
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy.orm import selectinload

from ai3d.models import GenerationJob, Image
from ai3d.models.enums import JobEventType, JobStatus
from ai3d.processing.mesh import validate_mesh_file
from ai3d.processing.photogrammetry import mesh_to_glb, prepare_photo
from ai3d.services.devices import cuda_available
from ai3d.services.models import save_reconstructed_model
from ai3d.workers import heavy
from ai3d.workers.runner import JobContext
from ai3d_engine.reconstruction import (
    STAGE_SPAN,
    ColmapCancelledError,
    ColmapNotFoundError,
    ColmapRunner,
    ReconstructionError,
    ReconstructionOptions,
    Stage,
    reconstruct,
)
from ai3d_engine.reconstruction.openmvs import (
    OpenMVSCancelledError,
    OpenMVSNotFoundError,
    check_openmvs,
)

log = logging.getLogger(__name__)

# Overall job progress: photo preparation before, saving after the COLMAP run.
_RUN_SPAN = (0.05, 0.92)
_HEARTBEAT_SECONDS = 2.0

_STAGE_MESSAGES: dict[Stage, str] = {
    "features": "Finding features in the photos.",
    "matching": "Matching features between photos.",
    "mapping": "Estimating camera positions and 3D points.",
    "filtering": "Orienting the model and removing stray points.",
    "meshing": "Building the surface.",
    "texturing": "Projecting the photos onto the surface.",
}


class _FailedError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class _CancelledError(Exception):
    pass


def _load(ctx: JobContext) -> GenerationJob:
    with ctx.session() as db:
        job = db.get(GenerationJob, ctx.job_id, options=[selectinload(GenerationJob.inputs)])
        if job is None:
            raise _FailedError("job_missing", "The job no longer exists.")
        db.expunge(job)
        return job


def _prepare_photos(ctx: JobContext, job: GenerationJob, images_dir: Path) -> None:
    storage = ctx.runner.storage
    images_dir.mkdir(parents=True)
    for item in job.inputs:
        with ctx.session() as db:
            source = db.get(Image, item.image_id) if item.image_id else None
            if source is None or source.sha256 != item.image_sha256:
                raise _FailedError(
                    "input_missing",
                    "A photo used by this job was deleted. Start a new reconstruction.",
                )
            key = source.storage_key
        local = storage.local_path(key)
        if local is None:
            local = images_dir.parent / f"source-{item.position}"
            with storage.open(key) as src, local.open("wb") as out:
                shutil.copyfileobj(src, out)
        try:
            prepare_photo(local, images_dir / f"{item.position:03d}")
        except OSError as exc:
            raise _FailedError("input_invalid", f"A photo could not be read: {exc}") from exc
    ctx.event(
        JobEventType.LOG,
        f"Prepared {len(job.inputs)} photos on this computer (upright, location data removed). "
        "Nothing is uploaded.",
        stage="preparing",
    )


def _options(ctx: JobContext, job: GenerationJob) -> ReconstructionOptions:
    settings = dict(job.params.get("provider_settings") or {})
    # OpenMVS is preferred when installed: it is the dense path verified on real photos.
    use_openmvs = check_openmvs() is None
    dense = (
        not use_openmvs
        and bool(settings.get("dense_if_cuda"))
        and cuda_available(ctx.settings.data_dir)
    )
    return ReconstructionOptions(
        max_image_size=int(settings.get("max_image_size", 2400)),
        max_num_features=int(settings.get("max_num_features", 8192)),
        matcher="exhaustive" if len(job.inputs) <= 150 else "sequential",
        dense=dense,
        openmvs=use_openmvs,
        openmvs_resolution_level=int(settings.get("openmvs_resolution_level", 1)),
        openmvs_decimate=float(settings.get("openmvs_decimate", 1.0)),
        texture=True,
        texture_scale=float(settings.get("texture_scale", 1.0)),
        step_timeout=float(ctx.settings.reconstruction_timeout_seconds),
    )


class _Progress:
    """Turns stage progress into job progress and events, without flooding either."""

    def __init__(self, ctx: JobContext) -> None:
        self.ctx = ctx
        self.stage: Stage | None = None
        self.reported = 0.0

    def __call__(self, stage: Stage, fraction: float, message: str | None) -> None:
        low, high = STAGE_SPAN[stage]
        run_fraction = low + (high - low) * min(max(fraction, 0.0), 1.0)
        overall = _RUN_SPAN[0] + (_RUN_SPAN[1] - _RUN_SPAN[0]) * run_fraction
        overall = max(overall, self.reported)  # never move backwards
        if stage != self.stage:
            self.stage = stage
            self.reported = overall
            self.ctx.update(stage=stage, progress=overall)
            self.ctx.event(
                JobEventType.STAGE,
                message or _STAGE_MESSAGES[stage],
                stage=stage,
                progress=overall,
            )
        elif overall - self.reported >= 0.02:
            self.reported = overall
            self.ctx.update(progress=overall)
            self.ctx.event(
                JobEventType.PROGRESS,
                f"{_STAGE_MESSAGES[stage].rstrip('.')}: {round(fraction * 100)}%",
                stage=stage,
                progress=overall,
            )


def _cancel_check(ctx: JobContext, deadline: float) -> Any:
    last = [0.0, False]

    def cancelled() -> bool:
        now = monotonic()
        if now > deadline:
            raise _FailedError(
                "timeout",
                f"The reconstruction did not finish within "
                f"{ctx.settings.reconstruction_timeout_seconds // 60} minutes.",
            )
        if now - last[0] >= _HEARTBEAT_SECONDS:
            last[0] = now
            last[1] = ctx.heartbeat()  # raises when the runner stops or the lease is lost
        return bool(last[1])

    return cancelled


def _wait_turn(ctx: JobContext) -> None:
    if not heavy.wait_turn(ctx, "Waiting for another local job on this computer to finish."):
        raise _CancelledError


def _run(ctx: JobContext, job: GenerationJob, workdir: Path) -> None:
    ctx.stage("preparing", "Preparing photos on this computer.", 0.02)
    images_dir = workdir / "images"
    _prepare_photos(ctx, job, images_dir)
    options = _options(ctx, job)
    if options.openmvs:
        ctx.event(
            JobEventType.LOG,
            "The dense surface will be computed on the CPU with OpenMVS (several minutes).",
            stage="preparing",
        )
    elif not options.dense:
        ctx.event(
            JobEventType.LOG,
            "No OpenMVS and no CUDA GPU: the surface will be built from sparse points, which "
            "is coarse on real photos.",
            stage="preparing",
        )
    deadline = monotonic() + ctx.settings.reconstruction_timeout_seconds
    result = reconstruct(
        images_dir,
        workdir / "colmap",
        options,
        runner=ColmapRunner(),
        progress=_Progress(ctx),
        cancelled=_cancel_check(ctx, deadline),
    )
    for warning in result.warnings:
        ctx.event(JobEventType.WARNING, warning)
    for note in result.notes:
        ctx.event(JobEventType.LOG, note)

    ctx.stage("validating", "Checking the 3D file.", 0.94)
    glb_path = workdir / "model.glb"
    try:
        mesh_to_glb(result.mesh_path, result.transform, glb_path)
    except ValueError as exc:
        raise _FailedError("invalid_output", str(exc)) from exc
    report = validate_mesh_file(glb_path)
    if not report.usable:
        reason = "; ".join(i.message for i in report.issues if i.severity == "error")
        raise _FailedError("invalid_output", f"The reconstructed surface is not usable: {reason}")

    ctx.stage("saving", "Saving the model.", 0.98)
    with ctx.session() as db:
        version = save_reconstructed_model(
            db,
            ctx.runner.storage,
            job=job,
            glb_path=glb_path,
            points_path=result.points_path,
            report=report,
            result=result,
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
            f"Model ready: {len(result.registered)} of {result.total_images} photos placed, "
            f"{result.point_count:,} points, {report.metrics.get('triangle_count', 0):,} "
            "triangles.",
            stage="done",
            progress=1.0,
            data={"model_version_id": str(version.id), **result.metrics()},
        )


def run_reconstruction(ctx: JobContext) -> None:
    tmp_root = ctx.settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="reconstruct-", dir=tmp_root))
    acquired = False
    try:
        job = _load(ctx)
        if job.cancel_requested:
            raise _CancelledError
        _wait_turn(ctx)
        acquired = True
        _run(ctx, job, workdir)
    except (_CancelledError, ColmapCancelledError, OpenMVSCancelledError):
        if ctx.finish(JobStatus.CANCELLED, stage=None):
            ctx.event(JobEventType.STATUS, "Cancelled. The reconstruction was stopped.")
    except (ColmapNotFoundError, OpenMVSNotFoundError) as exc:
        _fail(ctx, "engine_unavailable", str(exc))
    except ReconstructionError as exc:
        _fail(ctx, exc.code, exc.message, exc.details)
    except _FailedError as exc:
        _fail(ctx, exc.code, exc.message, exc.details)
    finally:
        if acquired:
            heavy.release()
        shutil.rmtree(workdir, ignore_errors=True)


def _fail(ctx: JobContext, code: str, message: str, details: dict[str, Any] | None = None) -> None:
    if ctx.finish(
        JobStatus.FAILED, error_code=code, error_message=message, error_details=details or {}
    ):
        ctx.event(JobEventType.ERROR, message, data={"code": code})
    log.info("reconstruction failed", extra={"job_id": str(ctx.job_id), "code": code})
