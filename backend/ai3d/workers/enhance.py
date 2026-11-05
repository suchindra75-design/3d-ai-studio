"""Enhance job: process an existing version into a new one, on this machine.

loading → processing → validating → saving. The source version is read, never
written; nothing is uploaded. An interrupted run is simply repeated.
"""

import logging
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ai3d.models import GenerationJob, ModelVersion
from ai3d.models.enums import AssetKind, JobEventType, JobStatus
from ai3d.processing.geometry import (
    HeavyTurn,
    RemeshSettings,
    ScaleReference,
    Step,
    UpscaleLimits,
    process,
)
from ai3d.processing.mesh import load_model_scene, validate_mesh_file
from ai3d.services import resources
from ai3d.services.models import save_enhanced_version, version_file
from ai3d.workers import heavy
from ai3d.workers.runner import JobContext
from ai3d_engine.blender import BlenderCancelledError
from ai3d_engine.local_ai.engine import LocalAICancelledError

log = logging.getLogger(__name__)


class _FailedError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _load(ctx: JobContext) -> tuple[GenerationJob, ModelVersion, str]:
    with ctx.session() as db:
        job = db.get(GenerationJob, ctx.job_id)
        if job is None or job.source_model_version_id is None:
            raise _FailedError("job_missing", "The job or its source version no longer exists.")
        version, asset = version_file(db, job.source_model_version_id, AssetKind.MESH)
        db.expunge(job)
        db.expunge(version)
        return job, version, asset.storage_key


def _remesh_settings(raw: dict[str, Any] | None) -> RemeshSettings | None:
    if not raw:
        return None
    return RemeshSettings(
        method=raw["method"],
        target_faces=int(raw.get("target_faces") or 20_000),
        resolution=int(raw.get("resolution") or 300),
        maps=tuple(raw.get("maps") or ()),
    )


def _heavy_turn(ctx: JobContext) -> HeavyTurn:
    @contextmanager
    def turn() -> Iterator[None]:
        if not heavy.wait_turn(ctx, "Waiting for another local job to finish before baking."):
            raise _CancelledError
        try:
            yield
        finally:
            heavy.release()

    return turn


def _run(ctx: JobContext, workdir: Path) -> None:
    job, source, key = _load(ctx)
    ctx.stage("loading", f"Opening version {source.version_number}.", 0.05)
    path = workdir / "source.glb"
    with ctx.runner.storage.open(key) as src, path.open("wb") as copy:
        shutil.copyfileobj(src, copy)
    try:
        scene = load_model_scene(path)
    except Exception as exc:  # trimesh raises many types for unreadable input
        raise _FailedError("unreadable", "The source version could not be opened.") from exc

    if ctx.heartbeat():
        raise _CancelledError
    ctx.stage("processing", "Processing geometry and textures.", 0.2)
    scale_in: dict[str, Any] | None = job.params.get("scale")
    try:
        result = process(
            scene,
            job.quality_mode,
            remove_ground_plane=bool(job.params.get("remove_ground")),
            scale=ScaleReference(scale_in["axis"], float(scale_in["length_mm"]))
            if scale_in
            else None,
            workdir=workdir,
            cancel=ctx.heartbeat,  # polled while Blender runs (texture baking)
            heavy_turn=_heavy_turn(ctx),
            smoothing=int(job.params.get("smoothing") or 0),
            remesh_settings=_remesh_settings(job.params.get("remesh")),
            upscale=job.params.get("upscale_textures"),
            upscale_limits=UpscaleLimits(
                max_input_px=ctx.settings.upscale_max_input_px,
                tile_px=ctx.settings.upscale_tile_px,
                available_bytes=resources.machine(ctx.settings.data_dir).memory_available_bytes,
            ),
        )
    except ValueError as exc:
        raise _FailedError("invalid_input", str(exc)) from exc
    except (BlenderCancelledError, LocalAICancelledError):
        raise _CancelledError from None
    scene = result.scene
    for step in result.steps:
        ctx.event(
            JobEventType.LOG if step.status == "applied" else JobEventType.STATUS,
            f"{step.title}: {step.detail}",
            stage="processing",
        )
    if ctx.heartbeat():
        raise _CancelledError
    # What the user asked for but did not happen, with the reason (shown with the result).
    params = {**job.params, "warnings": not_applied(result.steps)}
    if not result.changed:
        if ctx.finish(
            JobStatus.SUCCEEDED, stage="done", progress=1.0, cancel_requested=False, params=params
        ):
            ctx.event(
                JobEventType.STATUS,
                "Nothing needed changing, so no new version was created.",
                stage="done",
                progress=1.0,
            )
        return

    ctx.stage("validating", "Checking the processed file.", 0.85)
    result_path = workdir / "enhanced.glb"
    result_path.write_bytes(scene.export(file_type="glb"))  # type: ignore[no-untyped-call]
    report = validate_mesh_file(result_path)
    if not report.usable:
        reason = "; ".join(i.message for i in report.issues if i.severity == "error")
        raise _FailedError("invalid_output", f"Processing produced an unusable model: {reason}")

    ctx.stage("saving", "Saving the new version.", 0.95)
    with ctx.session() as db:
        parent = db.get(ModelVersion, source.id)
        if parent is None:
            raise _FailedError("source_missing", "The source version was deleted meanwhile.")
        version = save_enhanced_version(
            db,
            ctx.runner.storage,
            job=job,
            parent=parent,
            glb_path=result_path,
            report=report,
            steps=[step.to_dict() for step in result.steps],
            scale=result.scale,
        )
        number = version.version_number
    if ctx.finish(
        JobStatus.SUCCEEDED,
        stage="done",
        progress=1.0,
        result_model_version_id=version.id,
        cancel_requested=False,
        params=params,
    ):
        ctx.event(
            JobEventType.STATUS,
            f"Saved as version {number}; version {source.version_number} is unchanged.",
            stage="done",
            progress=1.0,
            data={"model_version_id": str(version.id)},
        )


# Steps that run only when asked for; the others are routine checks that may well
# find nothing to do.
OPTIONAL_STEPS = frozenset(
    {"ground", "smoothing", "retopology", "maps", "baking", "upscale", "scale"}
)


def not_applied(steps: list[Step]) -> list[str]:
    """Requested steps that did not apply, as "Title: reason"."""
    return [
        f"{s.title}: {s.detail}"
        for s in steps
        if s.code in OPTIONAL_STEPS
        and s.status in {"skipped", "unavailable"}
        and s.detail != "Not requested."
    ]


class _CancelledError(Exception):
    pass


def run_enhance(ctx: JobContext) -> None:
    tmp_root = ctx.settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="enhance-", dir=tmp_root))
    try:
        _run(ctx, workdir)
    except _CancelledError:
        if ctx.finish(JobStatus.CANCELLED, stage=None):
            ctx.event(JobEventType.STATUS, "Cancelled. No new version was created.")
    except _FailedError as exc:
        if ctx.finish(JobStatus.FAILED, error_code=exc.code, error_message=exc.message):
            ctx.event(JobEventType.ERROR, exc.message, data={"code": exc.code})
        log.info("enhance failed", extra={"job_id": str(ctx.job_id), "code": exc.code})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
