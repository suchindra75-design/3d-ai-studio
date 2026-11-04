"""Persistent, database-backed job runner.

* Jobs are claimed with a compare-and-set update (``queued`` → ``running``),
  which works the same on SQLite and PostgreSQL without row locks.
* A claimed job carries a lease (``lease_owner``/``lease_expires_at``) that the
  worker extends on every heartbeat. A job whose lease expired belongs to a
  worker that died; :meth:`JobRunner.recover` re-queues it — resuming the
  existing provider task when one was created, so nothing is paid twice.
* Cancellation is cooperative: the API sets ``cancel_requested`` and the
  worker acts on it at its next heartbeat.
"""

import contextlib
import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from time import monotonic
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ai3d.core.config import Settings
from ai3d.core.timeutil import utcnow
from ai3d.database.session import Database
from ai3d.models import GenerationJob
from ai3d.models.enums import JobEventType, JobKind, JobStatus
from ai3d.services.jobs import add_event
from ai3d.services.providers import ProviderManager
from ai3d.storage import StorageBackend

log = logging.getLogger(__name__)


class RunnerStoppingError(Exception):
    """The application is shutting down; leave the job for recovery."""


class LeaseLostError(Exception):
    """Another worker owns the job now (or it was deleted)."""


class JobContext:
    """A worker's handle on one claimed job. Every write checks the lease."""

    def __init__(self, runner: "JobRunner", job_id: uuid.UUID) -> None:
        self.runner = runner
        self.job_id = job_id

    @property
    def settings(self) -> Settings:
        return self.runner.settings

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.runner.database.session() as db:
            yield db

    def _lease_values(self) -> dict[str, Any]:
        return {"lease_expires_at": utcnow() + timedelta(seconds=self.settings.job_lease_seconds)}

    def update(self, **values: Any) -> None:
        with self.session() as db:
            result = db.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == self.job_id,
                    GenerationJob.lease_owner == self.runner.worker_id,
                )
                .values(**values, **self._lease_values())
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise LeaseLostError(str(self.job_id))

    def event(
        self,
        type_: JobEventType,
        message: str,
        *,
        stage: str | None = None,
        progress: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self.session() as db:
            add_event(db, self.job_id, type_, message, stage=stage, progress=progress, data=data)

    def stage(self, stage: str, message: str, progress: float) -> None:
        self.update(stage=stage, progress=progress)
        self.event(JobEventType.STAGE, message, stage=stage, progress=progress)

    def heartbeat(self) -> bool:
        """Extend the lease. Returns True when cancellation was requested."""
        if self.runner.stopping:
            raise RunnerStoppingError
        with self.session() as db:
            row = db.execute(
                select(GenerationJob.cancel_requested, GenerationJob.lease_owner).where(
                    GenerationJob.id == self.job_id
                )
            ).one_or_none()
            if row is None or row.lease_owner != self.runner.worker_id:
                raise LeaseLostError(str(self.job_id))
            db.execute(
                update(GenerationJob)
                .where(GenerationJob.id == self.job_id)
                .values(**self._lease_values())
            )
            return bool(row.cancel_requested)

    def sleep(self, seconds: float) -> None:
        if self.runner.wait_stop(seconds):
            raise RunnerStoppingError

    def finish(self, status: JobStatus, **values: Any) -> bool:
        """Final transition; releases the lease. False if the lease was lost."""
        with self.session() as db:
            result = db.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == self.job_id,
                    GenerationJob.lease_owner == self.runner.worker_id,
                )
                .values(
                    status=status,
                    finished_at=utcnow() if status.is_terminal else None,
                    lease_owner=None,
                    lease_expires_at=None,
                    **values,
                )
            )
            return result.rowcount == 1  # type: ignore[attr-defined,no-any-return]

    def release(self) -> None:
        """Give the job back without finishing it (shutdown): recovery resumes it."""
        with self.session() as db:
            db.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == self.job_id,
                    GenerationJob.lease_owner == self.runner.worker_id,
                )
                .values(lease_expires_at=utcnow())
            )


JobHandler = Callable[[JobContext], None]


class JobRunner:
    def __init__(
        self,
        *,
        database: Database,
        storage: StorageBackend,
        settings: Settings,
        providers: ProviderManager,
        worker_id: str | None = None,
    ) -> None:
        self.database = database
        self.storage = storage
        self.settings = settings
        self.providers = providers
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._active: dict[uuid.UUID, Future[None]] = {}
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def wait_stop(self, seconds: float) -> bool:
        return self._stop.wait(seconds) if seconds > 0 else self._stop.is_set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(
            max_workers=self.settings.max_concurrent_jobs, thread_name_prefix="ai3d-job"
        )
        self._thread = threading.Thread(target=self._loop, name="ai3d-job-dispatcher", daemon=True)
        self._thread.start()
        log.info("job runner started", extra={"worker_id": self.worker_id})

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        deadline = monotonic() + timeout
        for future in list(self._active.values()):
            # Errors were already handled and logged by ``execute``.
            with contextlib.suppress(Exception):
                future.result(timeout=max(0.0, deadline - monotonic()))
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def _loop(self) -> None:
        sweep_every = max(2.0, self.settings.job_lease_seconds / 3)
        last_sweep = 0.0
        while not self._stop.is_set():
            try:
                if monotonic() - last_sweep >= sweep_every:
                    self.recover()
                    last_sweep = monotonic()
                self._dispatch()
            except Exception:
                log.exception("job dispatcher error")
            self._wake.wait(timeout=1.0)
            self._wake.clear()

    def _dispatch(self) -> None:
        assert self._pool is not None
        with self._lock:
            for finished in [j for j, f in self._active.items() if f.done()]:
                del self._active[finished]
            free = self.settings.max_concurrent_jobs - len(self._active)
        while free > 0 and not self._stop.is_set():
            job_id = self.claim_next()
            if job_id is None:
                return
            future = self._pool.submit(self.execute, job_id)
            with self._lock:
                self._active[job_id] = future
            future.add_done_callback(lambda _f: self._wake.set())
            free -= 1

    # -- claiming & recovery ------------------------------------------------------

    def claim_next(self) -> uuid.UUID | None:
        now = utcnow()
        with self.database.session() as db:
            candidates = db.scalars(
                select(GenerationJob.id)
                .where(
                    GenerationJob.status == JobStatus.QUEUED,
                    or_(GenerationJob.queued_at.is_(None), GenerationJob.queued_at <= now),
                )
                .order_by(GenerationJob.queued_at, GenerationJob.created_at)
                .limit(10)
            ).all()
            for job_id in candidates:
                result = db.execute(
                    update(GenerationJob)
                    .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.QUEUED)
                    .values(
                        status=JobStatus.RUNNING,
                        lease_owner=self.worker_id,
                        lease_expires_at=now + timedelta(seconds=self.settings.job_lease_seconds),
                        started_at=now,
                        attempt=GenerationJob.attempt + 1,
                    )
                )
                if result.rowcount == 1:  # type: ignore[attr-defined]
                    db.commit()
                    return job_id
        return None

    def recover(self) -> int:
        """Re-queue or fail jobs whose worker disappeared. Returns how many were handled."""
        now = utcnow()
        handled = 0
        with self.database.session() as db:
            stale = list(
                db.scalars(
                    select(GenerationJob).where(
                        GenerationJob.status.in_([JobStatus.RUNNING, JobStatus.CANCELLING]),
                        or_(
                            GenerationJob.lease_expires_at.is_(None),
                            GenerationJob.lease_expires_at < now,
                        ),
                    )
                )
            )
            for job in stale:
                with self._lock:
                    if job.id in self._active:
                        continue
                handled += self._recover_one(db, job, now)
        return handled

    def _recover_one(self, db: Session, job: GenerationJob, now: Any) -> int:
        guard = (
            GenerationJob.id == job.id,
            GenerationJob.status == job.status,
            GenerationJob.lease_owner.is_(None)
            if job.lease_owner is None
            else GenerationJob.lease_owner == job.lease_owner,
        )
        provider_name = job.provider_key or "the provider"
        if job.provider_task_id or job.cancel_requested or job.stage != "submitting":
            if (
                job.provider_task_id is None
                and not job.cancel_requested
                and job.attempt >= job.max_attempts
            ):
                values: dict[str, Any] = {
                    "status": JobStatus.FAILED,
                    "error_code": "interrupted",
                    "error_message": "The job was interrupted too many times.",
                    "finished_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
                message = values["error_message"]
                event_type = JobEventType.ERROR
            else:
                values = {
                    "status": JobStatus.QUEUED,
                    "queued_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
                if job.kind == JobKind.PHOTOGRAMMETRY:
                    message = (
                        "Restarting the local reconstruction after an interruption "
                        "(it runs on this computer; nothing is uploaded or charged)."
                    )
                elif job.kind == JobKind.ENHANCE:
                    message = "Restarting the enhancement after an interruption."
                elif job.provider_task_id:
                    message = (
                        f"Resuming after an interruption: checking the existing {provider_name} "
                        "task (nothing is uploaded or charged again)."
                    )
                else:
                    message = "Resuming after an interruption (nothing had been sent yet)."
                event_type = JobEventType.STATUS
        else:
            # Interrupted while creating the provider task: it may exist and be
            # billed, so it is not resubmitted automatically.
            values = {
                "status": JobStatus.FAILED,
                "error_code": "interrupted",
                "error_message": (
                    f"AI 3D Studio stopped while sending photos to {provider_name}. The task may "
                    f"have been created; check your {provider_name} dashboard before retrying."
                ),
                "finished_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
            }
            message = values["error_message"]
            event_type = JobEventType.ERROR
        result = db.execute(update(GenerationJob).where(*guard).values(**values))
        if result.rowcount != 1:  # type: ignore[attr-defined]
            db.rollback()
            return 0
        db.commit()
        add_event(db, job.id, event_type, message)
        log.info(
            "job recovered",
            extra={"job_id": str(job.id), "new_status": str(values["status"])},
        )
        self._wake.set()
        return 1

    # -- execution ----------------------------------------------------------------

    def execute(self, job_id: uuid.UUID) -> None:
        from ai3d.workers.enhance import run_enhance  # noqa: PLC0415
        from ai3d.workers.generation import run_generation  # noqa: PLC0415 - avoids a cycle
        from ai3d.workers.reconstruction import run_reconstruction  # noqa: PLC0415

        context = JobContext(self, job_id)
        with self.database.session() as db:
            kind = db.scalar(select(GenerationJob.kind).where(GenerationJob.id == job_id))
        handlers = {JobKind.PHOTOGRAMMETRY: run_reconstruction, JobKind.ENHANCE: run_enhance}
        handler = handlers.get(kind, run_generation) if kind else run_generation
        try:
            handler(context)
        except RunnerStoppingError:
            context.release()
            log.info("job left for recovery at shutdown", extra={"job_id": str(job_id)})
        except LeaseLostError:
            log.warning("job lease lost", extra={"job_id": str(job_id)})
        except Exception:
            log.exception("job crashed", extra={"job_id": str(job_id)})
            try:
                if context.finish(
                    JobStatus.FAILED,
                    error_code="internal_error",
                    error_message="An unexpected error stopped the job. See the backend log.",
                ):
                    context.event(JobEventType.ERROR, "An unexpected error stopped the job.")
            except Exception:
                log.exception("could not mark job as failed", extra={"job_id": str(job_id)})

    def run_until_idle(self, max_jobs: int = 100) -> int:
        """Claim and run queued jobs on the calling thread (tests, CLI). Returns the count."""
        count = 0
        while count < max_jobs:
            job_id = self.claim_next()
            if job_id is None:
                break
            self.execute(job_id)
            count += 1
        return count
