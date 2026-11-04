"""Generation jobs: planning, creation, consent, cancellation, retry, events.

Routing rules that are never relaxed (docs/AI_PIPELINE.md §6):

* ``processing_mode=local`` never contacts a cloud provider.
* A job that would send photos to a third party runs only after explicit
  consent for that job, in a project that allows cloud upload. Otherwise it
  waits in ``waiting_for_consent``; nothing is uploaded and nothing falls back.
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ai3d.core.errors import AppError, ConflictError, InvalidInputError, NotFoundError
from ai3d.core.timeutil import utcnow
from ai3d.models import GenerationJob, Image, JobEvent, JobInput, ModelVersion, Project
from ai3d.models.enums import (
    ImageKind,
    JobEventType,
    JobKind,
    JobStatus,
    ProcessingMode,
    ProjectStatus,
    QualityLevel,
    QualityMode,
)
from ai3d.providers import Provider
from ai3d.providers.base import VIEW_ORDER, ProviderPlan, ViewRole
from ai3d.providers.local_ai import LocalAIProvider
from ai3d.providers.local_photogrammetry import LocalPhotogrammetryProvider
from ai3d.schemas.common import ErrorBody
from ai3d.schemas.job import (
    CostEstimateRead,
    GenerationCreate,
    GenerationPlanRead,
    JobCostRead,
    JobEventRead,
    JobInputRead,
    JobRead,
    PlanIssue,
    PlannedInputRead,
    ProviderSummary,
)
from ai3d.services.capture import PHOTOGRAMMETRY_MIN, PHOTOGRAMMETRY_RECOMMENDED
from ai3d.services.devices import cuda_available
from ai3d.services.projects import get_project
from ai3d.services.providers import ProviderManager
from ai3d_engine.reconstruction.openmvs import check_openmvs

log = logging.getLogger(__name__)

ConsentReason = Literal["project_local_only", "confirmation_required"]


# -- events -------------------------------------------------------------------


def add_event(
    db: Session,
    job_id: uuid.UUID,
    type_: JobEventType,
    message: str,
    *,
    stage: str | None = None,
    progress: float | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Append an event and commit. Callers commit their own changes first.

    The API and the worker may append concurrently; a clash on ``seq`` is
    retried with the next number.
    """
    for _attempt in range(5):
        seq = (db.scalar(select(func.max(JobEvent.seq)).where(JobEvent.job_id == job_id)) or 0) + 1
        db.add(
            JobEvent(
                job_id=job_id,
                seq=seq,
                type=type_,
                stage=stage,
                message=message,
                progress=progress,
                data=data,
            )
        )
        try:
            db.commit()
            return
        except IntegrityError:
            db.rollback()
    log.warning("could not record job event", extra={"job_id": str(job_id)})


def list_events(db: Session, job_id: uuid.UUID, after: int = 0) -> list[JobEvent]:
    return list(
        db.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.seq > after)
            .order_by(JobEvent.seq)
            .limit(500)
        )
    )


# -- planning -----------------------------------------------------------------


@dataclass
class PlannedInput:
    image: Image
    role: ViewRole


@dataclass
class Plan:
    project: Project
    quality_mode: QualityMode
    processing_mode: ProcessingMode
    inputs: list[PlannedInput]
    kind: JobKind
    provider: type[Provider] | None = None
    provider_plan: ProviderPlan | None = None
    requires_consent: bool = False
    consent_reason: ConsentReason | None = None
    warnings: list[str] = field(default_factory=list)
    blocking: list[PlanIssue] = field(default_factory=list)

    def block(self, code: str, message: str) -> None:
        self.blocking.append(PlanIssue(code=code, message=message))


def _resolve_inputs(
    db: Session, project: Project, body: GenerationCreate, kind: JobKind
) -> list[PlannedInput]:
    ids = [item.image_id for item in body.inputs]
    if len(set(ids)) != len(ids):
        raise InvalidInputError("Each photo can be used only once per generation.")
    images = {
        image.id: image
        for image in db.scalars(
            select(Image).where(
                Image.id.in_(ids),
                Image.project_id == project.id,
                Image.kind == ImageKind.ORIGINAL,
            )
        )
    }
    missing = [str(i) for i in ids if i not in images]
    if missing:
        raise NotFoundError("Photo not found in this project.", details={"image_ids": missing})

    if kind == JobKind.PHOTOGRAMMETRY:
        # Photogrammetry finds each photo's position itself; view labels do not apply.
        return [PlannedInput(images[i], "source") for i in ids]
    roles = [item.role for item in body.inputs]
    if len(roles) > len(VIEW_ORDER):
        raise InvalidInputError(
            f"AI multi-view generation takes at most {len(VIEW_ORDER)} photos. Use local "
            "photogrammetry for larger photo sets."
        )
    if len(roles) == 1:
        return [PlannedInput(images[ids[0]], "source")]
    if all(role == "source" for role in roles):
        # Unlabelled multi-view input: first photo is the front, then left, back, right.
        roles = list(VIEW_ORDER[: len(roles)])
    if "source" in roles or len(set(roles)) != len(roles):
        raise InvalidInputError(
            "With several photos, give each a different view: front, left, back or right."
        )
    return [PlannedInput(images[i], role) for i, role in zip(ids, roles, strict=True)]


def _choose_kind(
    manager: ProviderManager, body: GenerationCreate, processing_mode: ProcessingMode
) -> JobKind:
    count = len(body.inputs)
    photogrammetry = LocalPhotogrammetryProvider.key
    if body.provider == photogrammetry:
        return JobKind.PHOTOGRAMMETRY
    if count == 1:
        return JobKind.IMAGE_TO_3D
    if body.provider is not None:
        return JobKind.MULTI_VIEW_TO_3D
    if processing_mode == ProcessingMode.LOCAL:
        return JobKind.PHOTOGRAMMETRY
    # In Automatic mode a real capture set is better measured than invented (and AI
    # multi-view takes at most four photos).
    capture_set = count >= PHOTOGRAMMETRY_MIN or count > len(VIEW_ORDER)
    if (
        processing_mode == ProcessingMode.AUTO
        and capture_set
        and manager.unavailable_reason(photogrammetry) is None
    ):
        return JobKind.PHOTOGRAMMETRY
    return JobKind.MULTI_VIEW_TO_3D


def _plan_photogrammetry(manager: ProviderManager, plan: Plan) -> None:
    cls = LocalPhotogrammetryProvider
    reason = manager.unavailable_reason(cls.key)
    if reason is not None:
        plan.block("engine_unavailable", reason)
        return
    limits = cls.capabilities().photogrammetry
    assert limits is not None
    count = len(plan.inputs)
    if count < limits.min_images:
        plan.block(
            "too_few_photos",
            f"Photogrammetry needs at least {limits.min_images} photos of the object from "
            f"different angles ({PHOTOGRAMMETRY_MIN}+ for a usable result). It cannot recover "
            "shape from a single photo.",
        )
        return
    if count > limits.max_images:
        plan.block("too_many_photos", f"Use at most {limits.max_images} photos.")
        return
    plan.provider = cls
    if count < PHOTOGRAMMETRY_MIN:
        plan.warnings.append(
            f"Only {count} photos. {PHOTOGRAMMETRY_MIN}+ photos ({PHOTOGRAMMETRY_RECOMMENDED}+ "
            "recommended) with 60–80% overlap give a far more complete result."
        )
    if check_openmvs() is None:
        plan.warnings.append(
            "The dense surface is computed on the CPU with OpenMVS: expect several minutes "
            "(about 7 for 67 photos on a 6-core computer)."
        )
    elif not cuda_available(manager.settings.data_dir):
        plan.warnings.append(
            "No OpenMVS and no NVIDIA CUDA GPU, so the surface will be built from sparse points: "
            "the cameras are measured, but on real photos the surface is coarse and spiky. "
            "Install OpenMVS (docs/LOCAL_GPU_SETUP.md) for a dense surface."
        )
    if plan.processing_mode == ProcessingMode.AUTO:
        plan.warnings.append(
            "Automatic mode chose local photogrammetry: your photos stay on this computer."
        )


def _no_local_engine_message(manager: ProviderManager, kind: JobKind) -> str:
    if kind == JobKind.IMAGE_TO_3D:
        reason = manager.unavailable_reason(LocalAIProvider.key)
        return (
            "Turning a single photo into 3D on this computer needs the local AI engine. "
            f"{reason or ''} Local photogrammetry works from many overlapping photos; "
            "otherwise choose Cloud or Automatic processing."
        ).replace("  ", " ")
    return (
        "No local engine turns 2–4 photos into 3D. Local photogrammetry needs "
        f"{PHOTOGRAMMETRY_MIN}+ overlapping photos; otherwise choose Cloud or Automatic "
        "processing."
    )


def _choose_provider(
    db: Session, manager: ProviderManager, plan: Plan, requested: str | None
) -> None:
    if plan.kind == JobKind.PHOTOGRAMMETRY:
        _plan_photogrammetry(manager, plan)
    else:
        _choose_ai_provider(db, manager, plan, requested)


def _use_requested_provider(
    db: Session, manager: ProviderManager, plan: Plan, requested: str
) -> None:
    local_only = plan.processing_mode == ProcessingMode.LOCAL
    cls = manager.provider_class(requested)
    if local_only and cls.uploads_images:
        plan.block(
            "local_only",
            f"Local processing never sends photos to {cls.display_name}. Choose Cloud or "
            "Automatic processing to use it.",
        )
        return
    if not cls.capabilities().supports(plan.kind):
        plan.block(
            "unsupported_input",
            f"{cls.display_name} does not support this kind of input.",
        )
        return
    if not manager.is_configured(db, cls.key):
        plan.block(
            "provider_not_configured",
            manager.unavailable_reason(cls.key)
            or f"{cls.display_name} is not configured. Add an API key in Settings → AI providers.",
        )
        return
    plan.provider = cls


def _choose_ai_provider(
    db: Session, manager: ProviderManager, plan: Plan, requested: str | None
) -> None:
    local_only = plan.processing_mode == ProcessingMode.LOCAL
    if requested is not None:
        _use_requested_provider(db, manager, plan, requested)
        return

    if plan.processing_mode != ProcessingMode.CLOUD:
        local = [
            cls
            for cls in manager.registry.all()
            if not cls.uploads_images
            and cls.capabilities().supports(plan.kind)
            and manager.is_configured(db, cls.key)
        ]
        if local:
            plan.provider = local[0]
            if plan.processing_mode == ProcessingMode.AUTO:
                plan.warnings.append(
                    f"Automatic mode chose {plan.provider.display_name}: your photo stays on "
                    "this computer. Choose Cloud for a provider such as Tripo or Meshy."
                )
            return
    if local_only:
        plan.block(
            "no_local_engine",
            _no_local_engine_message(manager, plan.kind),
        )
        return
    candidates = [
        cls
        for cls in manager.registry.all()
        if cls.uploads_images
        and cls.capabilities().supports(plan.kind)
        and manager.is_configured(db, cls.key)
    ]
    if not candidates:
        plan.block(
            "no_provider",
            "No AI provider is configured. Add a Tripo or Meshy API key in Settings → AI "
            "providers.",
        )
        return
    plan.provider = candidates[0]
    if plan.processing_mode == ProcessingMode.AUTO:
        plan.warnings.append(
            f"Automatic mode chose {plan.provider.display_name} (cloud) because no local "
            "generation engine is available."
        )


def build_plan(
    db: Session, manager: ProviderManager, project_id: uuid.UUID, body: GenerationCreate
) -> Plan:
    project = get_project(db, project_id)
    processing_mode = body.processing_mode or project.default_processing_mode
    kind = _choose_kind(manager, body, processing_mode)
    plan = Plan(
        project=project,
        quality_mode=body.quality_mode or project.default_quality_mode,
        processing_mode=processing_mode,
        inputs=_resolve_inputs(db, project, body, kind),
        kind=kind,
    )
    if project.status == ProjectStatus.ARCHIVED:
        plan.block("project_archived", "Restore the project from the archive to generate models.")
    _choose_provider(db, manager, plan, body.provider)

    cls = plan.provider
    if cls is not None:
        caps = cls.capabilities()
        if plan.kind == JobKind.MULTI_VIEW_TO_3D and caps.multi_view is not None:
            limits = caps.multi_view
            if not limits.min_images <= len(plan.inputs) <= limits.max_images:
                plan.block(
                    "unsupported_input",
                    f"{cls.display_name} needs {limits.min_images}–{limits.max_images} views.",
                )
            roles = {item.role for item in plan.inputs}
            for role in limits.required_roles:
                if role not in roles:
                    plan.block("missing_view", f"{cls.display_name} needs a {role} view.")
        plan.provider_plan = cls.plan(plan.kind, plan.quality_mode)
        if plan.kind != JobKind.PHOTOGRAMMETRY:
            # The photogrammetry plan already states these facts for this machine.
            plan.warnings.extend(caps.notes)
        if cls.uploads_images:
            plan.requires_consent = True
            plan.consent_reason = (
                "confirmation_required" if project.allow_cloud_upload else "project_local_only"
            )

    if plan.kind == JobKind.PHOTOGRAMMETRY:
        plan.warnings.insert(
            0,
            "Photogrammetry measures the shape from overlapping photos. Areas no photo saw "
            "stay open, and the size is not to scale without a reference object.",
        )
    elif plan.kind == JobKind.IMAGE_TO_3D:
        plan.warnings.insert(
            0,
            "From one photo, the sides the camera did not see are invented by the AI. The "
            "result will look plausible but is not a measurement of the real object.",
        )
    else:
        plan.warnings.insert(
            0,
            "AI multi-view generation still invents surfaces between the views; it is not a "
            "measured reconstruction and its size is not to scale.",
        )
    poor = [
        i.image.original_filename for i in plan.inputs if i.image.quality_level == QualityLevel.POOR
    ]
    if poor:
        plan.warnings.append(f"Low-quality photo(s): {', '.join(poor)}. Results may suffer.")
    return plan


def plan_to_read(plan: Plan) -> GenerationPlanRead:
    cls = plan.provider
    pp = plan.provider_plan
    return GenerationPlanRead(
        ok=not plan.blocking,
        kind=plan.kind,
        quality_mode=plan.quality_mode,
        processing_mode=plan.processing_mode,
        route=("cloud" if cls.uploads_images else "local") if cls else None,
        provider=_summary(cls) if cls else None,
        inputs=[
            PlannedInputRead(
                image_id=i.image.id,
                role=i.role,
                filename=i.image.original_filename,
                quality_level=i.image.quality_level.value,
            )
            for i in plan.inputs
        ],
        provider_settings=pp.settings if pp else {},
        provider_summary=list(pp.summary) if pp else [],
        estimate=_estimate_read(pp),
        requires_consent=plan.requires_consent,
        consent_reason=plan.consent_reason,
        provenance=("measured" if plan.kind == JobKind.PHOTOGRAMMETRY else "inferred")
        if cls
        else None,
        warnings=plan.warnings,
        blocking_issues=plan.blocking,
    )


def _summary(cls: type[Provider]) -> ProviderSummary:
    return ProviderSummary(
        key=cls.key,
        display_name=cls.display_name,
        kind=cls.kind,
        uploads_images=cls.uploads_images,
    )


def _estimate_read(pp: ProviderPlan | None) -> CostEstimateRead | None:
    if pp is None or pp.estimate is None:
        return None
    e = pp.estimate
    return CostEstimateRead(
        amount=e.amount, unit=e.unit, usd=e.usd, basis=e.basis, notes=list(e.notes)
    )


# -- creation -----------------------------------------------------------------


def get_job(db: Session, job_id: uuid.UUID) -> GenerationJob:
    job = db.get(GenerationJob, job_id, options=[selectinload(GenerationJob.inputs)])
    if job is None:
        raise NotFoundError("Job not found.", details={"job_id": str(job_id)})
    return job


def _by_idempotency_key(db: Session, key: str, project_id: uuid.UUID) -> GenerationJob | None:
    job = db.scalar(select(GenerationJob).where(GenerationJob.idempotency_key == key))
    if job is not None and job.project_id != project_id:
        raise ConflictError("This Idempotency-Key was already used for another project.")
    return job


def create_generation(
    db: Session,
    manager: ProviderManager,
    project_id: uuid.UUID,
    body: GenerationCreate,
    *,
    idempotency_key: str | None = None,
    parent_job_id: uuid.UUID | None = None,
) -> tuple[GenerationJob, bool]:
    """Create a job. Returns ``(job, created)``; a repeated key returns the original job."""
    if idempotency_key:
        existing = _by_idempotency_key(db, idempotency_key, project_id)
        if existing is not None:
            return existing, False

    plan = build_plan(db, manager, project_id, body)
    if plan.blocking:
        first = plan.blocking[0]
        raise AppError(
            first.message,
            code=first.code,
            status_code=422,
            details={"issues": [issue.model_dump() for issue in plan.blocking]},
        )
    assert plan.provider is not None
    assert plan.provider_plan is not None
    cls = plan.provider
    credential = manager.credential(db, cls.key)
    consented = (
        plan.requires_consent and body.cloud_upload_consent and plan.project.allow_cloud_upload
    )
    ready = not plan.requires_consent or consented
    now = utcnow()
    job = GenerationJob(
        project_id=project_id,
        kind=plan.kind,
        status=JobStatus.QUEUED if ready else JobStatus.WAITING_FOR_CONSENT,
        quality_mode=plan.quality_mode,
        processing_mode=plan.processing_mode,
        provider_key=cls.key,
        provider_config_id=credential.config_id,
        params={
            "provider_settings": plan.provider_plan.settings,
            "provider_summary": list(plan.provider_plan.summary),
            "estimate": _estimate_read(plan.provider_plan).model_dump()  # type: ignore[union-attr]
            if plan.provider_plan.estimate
            else None,
            "warnings": plan.warnings,
            "consent_reason": None if ready else plan.consent_reason,
        },
        cloud_upload_consented=consented,
        idempotency_key=idempotency_key,
        parent_job_id=parent_job_id,
        queued_at=now if ready else None,
        inputs=[
            JobInput(position=n, image_id=i.image.id, image_sha256=i.image.sha256, role=i.role)
            for n, i in enumerate(plan.inputs)
        ],
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = _by_idempotency_key(db, idempotency_key, project_id)
            if existing is not None:
                return existing, False
        raise
    count = len(plan.inputs)
    photos = f"{count} photo{'s' if count != 1 else ''}"
    if ready:
        add_event(db, job.id, JobEventType.STATUS, f"Queued for {cls.display_name}.")
    else:
        add_event(
            db,
            job.id,
            JobEventType.STATUS,
            f"Waiting for your permission to send {photos} to {cls.display_name}. "
            "Nothing has been uploaded.",
        )
    log.info(
        "generation job created",
        extra={"job_id": str(job.id), "provider": cls.key, "status": job.status.value},
    )
    return job, True


# -- state changes ------------------------------------------------------------


def grant_consent(
    db: Session, job_id: uuid.UUID, *, allow_project_cloud_upload: bool
) -> GenerationJob:
    job = get_job(db, job_id)
    if job.status != JobStatus.WAITING_FOR_CONSENT:
        raise ConflictError("This job is not waiting for permission.")
    project = get_project(db, job.project_id)
    if not project.allow_cloud_upload:
        if not allow_project_cloud_upload:
            raise ConflictError(
                "This project is set to keep photos on this machine. Allow cloud upload for "
                "the project to continue.",
                code="project_local_only",
            )
        project.allow_cloud_upload = True
    result = db.execute(
        update(GenerationJob)
        .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.WAITING_FOR_CONSENT)
        .values(
            status=JobStatus.QUEUED,
            cloud_upload_consented=True,
            queued_at=utcnow(),
            params={**job.params, "consent_reason": None},
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        db.rollback()
        raise ConflictError("This job is not waiting for permission.")
    db.commit()
    add_event(db, job_id, JobEventType.STATUS, "Permission given. Queued.")
    db.refresh(job)
    return job


def cancel_job(db: Session, job_id: uuid.UUID) -> GenerationJob:
    for _attempt in range(3):
        job = get_job(db, job_id)
        if job.status.is_terminal:
            raise ConflictError("This job has already finished.")
        if job.status == JobStatus.CANCELLING:
            return job
        if job.status == JobStatus.QUEUED and job.provider_task_id:
            # Re-queued after a restart with a live provider task: a worker must
            # still ask the provider to cancel it.
            if job.cancel_requested:
                return job
            result = db.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.QUEUED)
                .values(cancel_requested=True)
            )
            message = "Cancellation requested."
        elif job.status in {JobStatus.QUEUED, JobStatus.WAITING_FOR_CONSENT}:
            result = db.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id, GenerationJob.status == job.status)
                .values(status=JobStatus.CANCELLED, finished_at=utcnow(), cancel_requested=True)
            )
            message = "Cancelled before anything was sent."
        else:  # running: the worker stops at its next check
            result = db.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.RUNNING)
                .values(status=JobStatus.CANCELLING, cancel_requested=True)
            )
            message = "Cancellation requested."
        if result.rowcount == 1:  # type: ignore[attr-defined]
            db.commit()
            add_event(db, job_id, JobEventType.STATUS, message)
            db.expire_all()
            return get_job(db, job_id)
        db.rollback()  # the status changed under us (e.g. a worker claimed it); re-read
    raise ConflictError("The job changed state; try again.")


def retry_job(
    db: Session,
    manager: ProviderManager,
    job_id: uuid.UUID,
    *,
    cloud_upload_consent: bool,
    idempotency_key: str | None = None,
) -> tuple[GenerationJob, bool]:
    original = get_job(db, job_id)
    if original.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
        raise ConflictError("Only failed or cancelled jobs can be retried.")
    if original.kind == JobKind.ENHANCE:
        from ai3d.services.enhance import retry_enhance  # noqa: PLC0415 - avoids a cycle

        return retry_enhance(db, original, idempotency_key=idempotency_key), True
    if any(item.image_id is None for item in original.inputs):
        raise InvalidInputError("A photo used by this job was deleted; start a new generation.")
    body = GenerationCreate(
        inputs=[{"image_id": item.image_id, "role": item.role} for item in original.inputs],
        provider=original.provider_key,
        quality_mode=original.quality_mode,
        processing_mode=original.processing_mode,
        cloud_upload_consent=cloud_upload_consent,
    )
    return create_generation(
        db,
        manager,
        original.project_id,
        body,
        idempotency_key=idempotency_key,
        parent_job_id=original.id,
    )


# -- reading ------------------------------------------------------------------


def list_jobs(db: Session, project_id: uuid.UUID, limit: int = 50) -> list[GenerationJob]:
    get_project(db, project_id)
    return list(
        db.scalars(
            select(GenerationJob)
            .where(GenerationJob.project_id == project_id)
            .options(selectinload(GenerationJob.inputs))
            .order_by(GenerationJob.created_at.desc())
            .limit(limit)
        )
    )


def to_read(db: Session, manager: ProviderManager, job: GenerationJob) -> JobRead:
    params = job.params or {}
    cls = manager.registry.get(job.provider_key) if job.provider_key else None
    provider = _summary(cls) if cls else None
    result_model_id = None
    if job.result_model_version_id:
        result_model_id = db.scalar(
            select(ModelVersion.model_id).where(ModelVersion.id == job.result_model_version_id)
        )
    error = None
    if job.error_code:
        error = ErrorBody(
            code=job.error_code,
            message=job.error_message or "",
            details=job.error_details or {},
        )
    estimate = params.get("estimate")
    cost = job.cost
    return JobRead(
        id=job.id,
        project_id=job.project_id,
        kind=job.kind,
        status=job.status,
        stage=job.stage,
        progress=job.progress,
        quality_mode=job.quality_mode,
        processing_mode=job.processing_mode,
        provider=provider,
        provider_task_id=job.provider_task_id,
        inputs=[JobInputRead.model_validate(i) for i in job.inputs],
        provider_settings=params.get("provider_settings") or {},
        provider_summary=params.get("provider_summary") or [],
        estimate=CostEstimateRead.model_validate(estimate) if estimate else None,
        cost=JobCostRead(
            credits=cost.get("credits"), usd=cost.get("usd"), source=cost.get("source", "provider")
        )
        if cost
        else None,
        warnings=params.get("warnings") or [],
        cloud_upload_consented=job.cloud_upload_consented,
        consent_reason=params.get("consent_reason"),
        cancel_requested=job.cancel_requested,
        error=error,
        attempt=job.attempt,
        parent_job_id=job.parent_job_id,
        result_model_version_id=job.result_model_version_id,
        source_model_version_id=job.source_model_version_id,
        result_model_id=result_model_id,
        can_cancel=not job.status.is_terminal and job.status != JobStatus.CANCELLING,
        can_retry=job.status in {JobStatus.FAILED, JobStatus.CANCELLED},
        created_at=job.created_at,
        updated_at=job.updated_at,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def event_to_read(event: JobEvent) -> JobEventRead:
    return JobEventRead.model_validate(event)
