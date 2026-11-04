import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ai3d.api.deps import DbDep, ProvidersDep, RunnerDep
from ai3d.core.errors import InvalidInputError
from ai3d.database.session import Database
from ai3d.models import GenerationJob
from ai3d.models.enums import JobStatus
from ai3d.schemas.job import (
    ConsentGrant,
    GenerationCreate,
    GenerationPlanRead,
    JobEventList,
    JobEventRead,
    JobList,
    JobRead,
    RetryRequest,
)
from ai3d.services import jobs as job_service

router = APIRouter(tags=["jobs"])

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=255,
        description="Repeating a request with the same key returns the original job.",
    ),
]

_SSE_POLL_SECONDS = 0.5
_SSE_KEEPALIVE_SECONDS = 15.0


@router.post(
    "/projects/{project_id}/generations/plan",
    response_model=GenerationPlanRead,
    summary="Preview how a generation would run (provider, settings, cost, consent)",
    description="Nothing is created or sent. `blocking_issues` explains why a generation "
    "cannot run as requested.",
)
def plan_generation(
    db: DbDep, manager: ProvidersDep, project_id: uuid.UUID, body: GenerationCreate
) -> GenerationPlanRead:
    return job_service.plan_to_read(job_service.build_plan(db, manager, project_id, body))


@router.post(
    "/projects/{project_id}/generations",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start an AI generation job",
    description="Jobs that would send photos to a third-party provider start only with "
    "`cloud_upload_consent: true` in a project that allows cloud upload; otherwise the job "
    "is created in `waiting_for_consent` and nothing is uploaded.",
)
def create_generation(
    *,
    db: DbDep,
    manager: ProvidersDep,
    runner: RunnerDep,
    response: Response,
    project_id: uuid.UUID,
    body: GenerationCreate,
    idempotency_key: IdempotencyKey = None,
) -> JobRead:
    job, created = job_service.create_generation(
        db, manager, project_id, body, idempotency_key=idempotency_key
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    runner.wake()
    return job_service.to_read(db, manager, job_service.get_job(db, job.id))


@router.get("/projects/{project_id}/jobs", response_model=JobList, summary="List a project's jobs")
def list_jobs(
    db: DbDep,
    manager: ProvidersDep,
    project_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JobList:
    jobs = job_service.list_jobs(db, project_id, limit)
    return JobList(items=[job_service.to_read(db, manager, j) for j in jobs], total=len(jobs))


@router.get("/jobs/{job_id}", response_model=JobRead, summary="Get a job")
def get_job(db: DbDep, manager: ProvidersDep, job_id: uuid.UUID) -> JobRead:
    return job_service.to_read(db, manager, job_service.get_job(db, job_id))


@router.post("/jobs/{job_id}/cancel", response_model=JobRead, summary="Cancel a job")
def cancel_job(db: DbDep, manager: ProvidersDep, runner: RunnerDep, job_id: uuid.UUID) -> JobRead:
    job = job_service.cancel_job(db, job_id)
    runner.wake()
    return job_service.to_read(db, manager, job)


@router.post(
    "/jobs/{job_id}/consent",
    response_model=JobRead,
    summary="Allow a waiting job to send its photos to the provider",
)
def grant_consent(
    db: DbDep, manager: ProvidersDep, runner: RunnerDep, job_id: uuid.UUID, body: ConsentGrant
) -> JobRead:
    job = job_service.grant_consent(
        db, job_id, allow_project_cloud_upload=body.allow_project_cloud_upload
    )
    runner.wake()
    return job_service.to_read(db, manager, job)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
    summary="Run a failed or cancelled job again as a new job",
)
def retry_job(
    *,
    db: DbDep,
    manager: ProvidersDep,
    runner: RunnerDep,
    response: Response,
    job_id: uuid.UUID,
    body: RetryRequest,
    idempotency_key: IdempotencyKey = None,
) -> JobRead:
    job, created = job_service.retry_job(
        db,
        manager,
        job_id,
        cloud_upload_consent=body.cloud_upload_consent,
        idempotency_key=idempotency_key,
        provider=body.provider,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    runner.wake()
    return job_service.to_read(db, manager, job_service.get_job(db, job.id))


@router.get("/jobs/{job_id}/events", response_model=JobEventList, summary="Job timeline")
def list_events(
    db: DbDep, job_id: uuid.UUID, after: Annotated[int, Query(ge=0)] = 0
) -> JobEventList:
    job = job_service.get_job(db, job_id)
    events = job_service.list_events(db, job_id, after)
    return JobEventList(items=[job_service.event_to_read(e) for e in events], job_status=job.status)


def _fetch(
    database: Database, job_id: uuid.UUID, after: int
) -> tuple[list[JobEventRead], JobStatus | None]:
    with database.session() as db:
        job_status = db.get(GenerationJob, job_id)
        if job_status is None:
            return [], None
        events = [job_service.event_to_read(e) for e in job_service.list_events(db, job_id, after)]
        return events, job_status.status


def _ensure_job(database: Database, job_id: uuid.UUID) -> None:
    with database.session() as db:
        job_service.get_job(db, job_id)


def _parse_last_event_id(value: str | None) -> int:
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError as exc:
        raise InvalidInputError("Last-Event-ID must be an event sequence number.") from exc


@router.get(
    "/jobs/{job_id}/stream",
    summary="Live job events (Server-Sent Events)",
    description="Each event's `id` is its sequence number; reconnect with `Last-Event-ID` "
    "(or `?after=`) to resume. The stream ends with an `end` event once the job has "
    "finished.",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Event stream"}},
)
async def stream_events(
    request: Request,
    job_id: uuid.UUID,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    database: Database = request.app.state.db
    start = after if after is not None else _parse_last_event_id(last_event_id)
    # Fail fast with the normal 404 body for unknown jobs.
    await run_in_threadpool(_ensure_job, database, job_id)

    async def generate() -> AsyncIterator[str]:
        last = start
        last_sent = monotonic()
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                return
            events, job_status = await run_in_threadpool(_fetch, database, job_id, last)
            for event in events:
                last = event.seq
                last_sent = monotonic()
                data = event.model_dump_json()
                yield f"id: {event.seq}\nevent: {event.type.value}\ndata: {data}\n\n"
            if job_status is None or (job_status.is_terminal and not events):
                payload = json.dumps({"status": job_status.value if job_status else None})
                yield f"event: end\ndata: {payload}\n\n"
                return
            if monotonic() - last_sent > _SSE_KEEPALIVE_SECONDS:
                last_sent = monotonic()
                yield ": keep-alive\n\n"
            await asyncio.sleep(_SSE_POLL_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        # no-transform stops proxies (including the Next.js dev/prod server) from
        # compressing the stream, which would hold events back until it closes.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
