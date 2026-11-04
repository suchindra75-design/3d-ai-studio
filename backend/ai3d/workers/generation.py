"""The image-to-3D generation pipeline run by the job worker.

preparing → submitting → generating (poll) → downloading → validating → saving

The provider task id is committed the moment it exists, so a restart resumes
polling the same task instead of paying for a new one.
"""

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy.orm import selectinload

from ai3d.core.errors import AppError
from ai3d.models import GenerationJob, Image
from ai3d.models.enums import JobEventType, JobStatus
from ai3d.processing.mesh import validate_mesh_file
from ai3d.providers import Provider
from ai3d.providers.base import (
    GenerationRequest,
    ProviderError,
    ProviderErrorCode,
    ProviderInput,
    TaskStatus,
)
from ai3d.services.images import ensure_provider_copy
from ai3d.services.models import save_generated_model
from ai3d.services.providers import ProviderNotConfiguredError
from ai3d.workers.runner import JobContext

log = logging.getLogger(__name__)

# Overall progress ranges per stage.
_GENERATING = (0.1, 0.9)
MAX_SUBMIT_ATTEMPTS = 4
MAX_POLL_FAILURES = 8
MAX_BACKOFF = 60.0


class JobFailedError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class JobCancelledError(Exception):
    def __init__(self, message: str, *, warning: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.warning = warning


def _load(ctx: JobContext) -> GenerationJob:
    with ctx.session() as db:
        job = db.get(GenerationJob, ctx.job_id, options=[selectinload(GenerationJob.inputs)])
        if job is None:
            raise JobFailedError("job_missing", "The job no longer exists.")
        db.expunge(job)
        return job


def _prepare_inputs(
    ctx: JobContext, job: GenerationJob, provider: type[Provider], workdir: Path
) -> list[ProviderInput]:
    caps = provider.capabilities()
    inputs: list[ProviderInput] = []
    resized = 0
    for item in job.inputs:
        with ctx.session() as db:
            source = db.get(Image, item.image_id) if item.image_id else None
            if source is None or source.sha256 != item.image_sha256:
                raise JobFailedError(
                    "input_missing", "A photo used by this job was deleted. Start a new generation."
                )
            try:
                copy = ensure_provider_copy(
                    db, ctx.runner.storage, ctx.settings, source, max_bytes=caps.max_input_bytes
                )
            except (AppError, ValueError) as exc:
                raise JobFailedError(
                    "input_invalid",
                    f"{source.original_filename} could not be prepared: {exc}",
                ) from exc
            if copy.mime_type not in caps.accepted_mime_types:
                raise JobFailedError(
                    "input_invalid",
                    f"{provider.display_name} does not accept {copy.mime_type} images.",
                )
            resized += bool(copy.analysis.get("resized"))
            path = workdir / f"input-{item.position}.{copy.storage_key.rsplit('.', 1)[-1]}"
            with ctx.runner.storage.open(copy.storage_key) as src, path.open("wb") as out:
                shutil.copyfileobj(src, out)
            inputs.append(
                ProviderInput(
                    path=path,
                    mime_type=copy.mime_type,
                    role=item.role,  # type: ignore[arg-type]
                    sha256=copy.sha256,
                )
            )
    ctx.event(
        JobEventType.LOG,
        f"Prepared {len(inputs)} photo cop{'y' if len(inputs) == 1 else 'ies'} without metadata "
        f"(location, camera details removed){f'; {resized} resized' if resized else ''}.",
        stage="preparing",
        data={"metadata_removed": True, "resized": resized},
    )
    return inputs


def _backoff(attempt: int, retry_after: float | None, base: float) -> float:
    if retry_after is not None:
        return min(MAX_BACKOFF, retry_after)
    return min(MAX_BACKOFF, max(1.0, base) * 2.0**attempt)


def _submit(ctx: JobContext, provider: Provider, request: GenerationRequest) -> str:
    for attempt in range(MAX_SUBMIT_ATTEMPTS):
        if ctx.heartbeat():
            raise JobCancelledError("Cancelled before anything was sent.")
        try:
            return provider.submit(request)
        except ProviderError as exc:
            if not exc.retryable or attempt == MAX_SUBMIT_ATTEMPTS - 1:
                raise
            delay = _backoff(attempt, exc.retry_after, ctx.settings.provider_poll_interval)
            ctx.event(
                JobEventType.WARNING,
                f"{exc.message} Trying again in {delay:.0f} s.",
                stage="submitting",
            )
            ctx.sleep(delay)
    raise AssertionError("unreachable")


def _cancel_at_provider(ctx: JobContext, provider: Provider, task_id: str) -> JobCancelledError:
    name = provider.display_name
    try:
        outcome = provider.cancel(task_id)
    except ProviderError as exc:
        return JobCancelledError(
            f"Stopped. {name} could not be asked to cancel ({exc.message}); the task may still "
            "finish and be charged to your account.",
            warning=True,
        )
    if outcome == "cancelled":
        return JobCancelledError(f"Cancelled. {name} cancelled the task.")
    if outcome == "unsupported":
        return JobCancelledError(
            f"Stopped tracking the task. {name} has no way to cancel it, so it may still finish "
            "and be charged to your account.",
            warning=True,
        )
    return JobCancelledError(
        f"{name} had already started the task and cannot stop it; it will be charged. "
        "AI 3D Studio stopped tracking it.",
        warning=True,
    )


def _wait(ctx: JobContext, provider: Provider, task_id: str) -> TaskStatus:
    name = provider.display_name
    interval = ctx.settings.provider_poll_interval
    deadline = monotonic() + ctx.settings.job_timeout_seconds
    failures = 0
    last_state: str | None = None
    last_reported = -1.0
    low, high = _GENERATING
    while True:
        if ctx.heartbeat():
            raise _cancel_at_provider(ctx, provider, task_id)
        try:
            status = provider.poll(task_id)
            failures = 0
        except ProviderError as exc:
            if not exc.retryable:
                raise
            failures += 1
            if failures > MAX_POLL_FAILURES:
                raise
            if failures == 1:
                ctx.event(
                    JobEventType.WARNING,
                    f"{exc.message} Still trying; the task keeps running at {name}.",
                    stage="generating",
                )
            ctx.sleep(_backoff(failures, exc.retry_after, interval))
            continue

        if status.state == "succeeded":
            return status
        if status.state == "failed":
            error = status.error or ProviderError(
                ProviderErrorCode.TASK_FAILED, f"{name} reported that generation failed."
            )
            error.details.setdefault("cost", status.cost)
            raise error
        if status.state == "cancelled":
            raise JobCancelledError(f"The task was cancelled at {name}.", warning=True)

        progress = low + (high - low) * (status.progress or 0.0)
        if status.state != last_state:
            message = (
                f"Waiting in the {name} queue."
                if status.state == "queued"
                else f"{name} is generating the model."
            )
            ctx.update(progress=progress)
            ctx.event(JobEventType.STAGE, message, stage="generating", progress=progress)
            last_state = status.state
            last_reported = progress
        elif progress - last_reported >= 0.02:
            ctx.update(progress=progress)
            ctx.event(
                JobEventType.PROGRESS,
                f"{round((status.progress or 0) * 100)}% at {name}",
                stage="generating",
                progress=progress,
            )
            last_reported = progress
        if monotonic() > deadline:
            raise ProviderError(
                ProviderErrorCode.TIMEOUT,
                f"{name} did not finish within {ctx.settings.job_timeout_seconds // 60} minutes. "
                "The task may still complete in your provider dashboard.",
                retryable=False,
            )
        ctx.sleep(interval)


@dataclass
class _Tracking:
    """What the failure handlers need to know about a partly finished run."""

    task_id: str | None
    cost: dict[str, Any] | None = None


def _start_task(
    ctx: JobContext,
    job: GenerationJob,
    provider: Provider,
    provider_cls: type[Provider],
    workdir: Path,
) -> str:
    name = provider.display_name
    ctx.stage("preparing", "Preparing photos.", 0.02)
    inputs = _prepare_inputs(ctx, job, provider_cls, workdir)
    count = len(inputs)
    ctx.stage("submitting", f"Sending {count} photo{'s' if count != 1 else ''} to {name}.", 0.06)
    request = GenerationRequest(
        job_id=job.id,
        kind=job.kind,
        quality_mode=job.quality_mode,
        inputs=tuple(inputs),
        settings=dict(job.params.get("provider_settings") or {}),
    )
    task_id = _submit(ctx, provider, request)
    # Committed immediately: from here on a restart resumes this task.
    ctx.update(provider_task_id=task_id, progress=_GENERATING[0])
    ctx.event(
        JobEventType.LOG,
        f"{name} task created.",
        stage="submitting",
        data={"provider_task_id": task_id},
    )
    return task_id


def _finish_task(
    ctx: JobContext,
    job: GenerationJob,
    *,
    provider: Provider,
    provider_cls: type[Provider],
    tracking: _Tracking,
    workdir: Path,
) -> None:
    assert tracking.task_id is not None
    name = provider.display_name
    status = _wait(ctx, provider, tracking.task_id)
    tracking.cost = status.cost

    ctx.stage("downloading", f"Downloading the model from {name}.", 0.92)
    result = provider.download_result(tracking.task_id, status, workdir)

    ctx.stage("validating", "Checking the 3D file.", 0.95)
    report = validate_mesh_file(result.mesh_path, file_type=result.mesh_format)
    if not report.usable:
        reason = "; ".join(i.message for i in report.issues if i.severity == "error")
        raise ProviderError(
            ProviderErrorCode.INVALID_OUTPUT,
            f"{name} returned a file that is not a usable 3D model: {reason}",
            retryable=False,
            details={"issues": [i.to_dict() for i in report.issues]},
        )

    ctx.stage("saving", "Saving the model.", 0.98)
    if ctx.heartbeat():
        ctx.event(
            JobEventType.WARNING,
            "The model finished before the cancellation took effect, so it was kept.",
        )
    with ctx.session() as db:
        version = save_generated_model(
            db,
            ctx.runner.storage,
            ctx.settings,
            job=job,
            provider=provider_cls,
            result=result,
            report=report,
        )
    if ctx.finish(
        JobStatus.SUCCEEDED,
        stage="done",
        progress=1.0,
        result_model_version_id=version.id,
        cost=tracking.cost,
        cancel_requested=False,
    ):
        ctx.event(
            JobEventType.STATUS,
            f"Model ready ({report.metrics.get('triangle_count', 0):,} triangles).",
            stage="done",
            progress=1.0,
            data={"model_version_id": str(version.id), "report_status": report.status.value},
        )
    log.info("generation succeeded", extra={"job_id": str(job.id), "provider": provider_cls.key})


def _instantiate(ctx: JobContext, job: GenerationJob) -> tuple[type[Provider], Provider] | None:
    manager = ctx.runner.providers
    provider_cls = manager.registry.get(job.provider_key or "")
    if provider_cls is None:
        _fail(ctx, "provider_unavailable", f"Provider {job.provider_key!r} is not available.")
        return None
    if provider_cls.uploads_images and not job.cloud_upload_consented:
        # Never upload without permission, even if a job reached the queue somehow.
        if ctx.finish(JobStatus.WAITING_FOR_CONSENT, stage=None, queued_at=None):
            ctx.event(
                JobEventType.STATUS,
                f"Waiting for your permission to send photos to {provider_cls.display_name}.",
            )
        return None
    try:
        with ctx.session() as db:
            provider, _credential = manager.instantiate(db, provider_cls.key)
    except (ProviderNotConfiguredError, ProviderError) as exc:
        _fail(ctx, "provider_not_configured", exc.message)
        return None
    return provider_cls, provider


def run_generation(ctx: JobContext) -> None:
    job = _load(ctx)
    instantiated = _instantiate(ctx, job)
    if instantiated is None:
        return
    provider_cls, provider = instantiated

    tmp_root = ctx.settings.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="job-", dir=tmp_root))
    tracking = _Tracking(task_id=job.provider_task_id)
    try:
        if tracking.task_id is None:
            if job.cancel_requested:
                raise JobCancelledError("Cancelled before anything was sent.")
            tracking.task_id = _start_task(ctx, job, provider, provider_cls, workdir)
        else:
            ctx.event(
                JobEventType.LOG,
                f"Checking the existing {provider.display_name} task.",
                stage="generating",
            )
        _finish_task(
            ctx,
            job,
            provider=provider,
            provider_cls=provider_cls,
            tracking=tracking,
            workdir=workdir,
        )
    except JobCancelledError as exc:
        if ctx.finish(JobStatus.CANCELLED, stage=None, cost=tracking.cost):
            ctx.event(JobEventType.WARNING if exc.warning else JobEventType.STATUS, exc.message)
    except ProviderError as exc:
        details: dict[str, Any] = {k: v for k, v in exc.details.items() if k != "cost"}
        if tracking.task_id:
            details["provider_task_id"] = tracking.task_id
        if ctx.finish(
            JobStatus.FAILED,
            error_code=exc.code.value,
            error_message=exc.message,
            error_details=details,
            cost=tracking.cost or exc.details.get("cost"),
        ):
            ctx.event(JobEventType.ERROR, exc.message, data={"code": exc.code.value})
        log.info(
            "generation failed",
            extra={"job_id": str(job.id), "provider": provider_cls.key, "code": exc.code.value},
        )
    except JobFailedError as exc:
        _fail(ctx, exc.code, exc.message, exc.details)
    finally:
        provider.close()
        shutil.rmtree(workdir, ignore_errors=True)


def _fail(ctx: JobContext, code: str, message: str, details: dict[str, Any] | None = None) -> None:
    if ctx.finish(
        JobStatus.FAILED, error_code=code, error_message=message, error_details=details or {}
    ):
        ctx.event(JobEventType.ERROR, message, data={"code": code})
