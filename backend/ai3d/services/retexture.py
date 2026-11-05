"""Re-texturing a version with a cloud service (JobKind.RETEXTURE).

The model (its GLB) is uploaded to the provider, so the job always needs
permission first, exactly like sending photos. The result is saved as a new
``enhanced`` version; the source version is never changed. Tripo can only
re-texture models it generated (its upload API takes images only).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai3d.core.errors import AppError, InvalidInputError
from ai3d.core.timeutil import utcnow
from ai3d.models import GenerationJob, ModelVersion
from ai3d.models.enums import JobEventType, JobKind, JobStatus, ProcessingMode
from ai3d.schemas.enhance import RetextureRequest
from ai3d.services.jobs import _estimate_read, add_event
from ai3d.services.models import get_model, get_version
from ai3d.services.projects import get_project
from ai3d.services.providers import ProviderManager


def source_task_id(db: Session, version: ModelVersion, provider_key: str) -> str | None:
    """The provider's own task id for a version it generated, if any."""
    if version.provider_key != provider_key or version.job_id is None:
        return None
    job = db.get(GenerationJob, version.job_id)
    return job.provider_task_id if job and job.kind != JobKind.RETEXTURE else None


def create_retexture_job(
    db: Session,
    manager: ProviderManager,
    model_id: uuid.UUID,
    body: RetextureRequest,
) -> GenerationJob:
    model = get_model(db, model_id)
    version = get_version(db, body.source_version_id or model.current_version_id)  # type: ignore[arg-type]
    if version.model_id != model.id:
        raise InvalidInputError("That version belongs to a different model.")
    cls = manager.registry.get(body.provider)
    if cls is None or not cls.capabilities().supports(JobKind.RETEXTURE):
        raise InvalidInputError("This service cannot re-texture models.")
    if not manager.is_configured(db, cls.key):
        raise AppError(
            f"{cls.display_name} has no API key yet. Add one in Settings.",
            code="provider_not_configured",
            status_code=422,
        )
    caps = cls.capabilities()
    task = source_task_id(db, version, cls.key)
    if caps.retexture_own_models_only and task is None:
        raise InvalidInputError(
            f"{cls.display_name} can only re-texture models it generated; this version was not "
            f"made by {cls.display_name}."
        )
    if not task and not body.prompt.strip():
        raise InvalidInputError("Describe the texture you want.")
    project = get_project(db, model.project_id)
    plan = cls.plan(JobKind.RETEXTURE, body.quality_mode)
    consented = body.cloud_upload_consent and project.allow_cloud_upload
    reason = (
        None
        if consented
        else ("confirmation_required" if project.allow_cloud_upload else "project_local_only")
    )
    now = utcnow()
    job = GenerationJob(
        project_id=model.project_id,
        kind=JobKind.RETEXTURE,
        status=JobStatus.QUEUED if consented else JobStatus.WAITING_FOR_CONSENT,
        quality_mode=body.quality_mode,
        processing_mode=ProcessingMode.CLOUD,
        provider_key=cls.key,
        provider_config_id=manager.credential(db, cls.key).config_id,
        source_model_version_id=version.id,
        params={
            "model_id": str(model.id),
            "source_version_number": version.version_number,
            "provider_settings": {
                **plan.settings,
                "prompt": body.prompt.strip(),
                "source_task_id": task,
            },
            "provider_summary": list(plan.summary),
            "estimate": _estimate_read(plan).model_dump() if plan.estimate else None,  # type: ignore[union-attr]
            "warnings": [*plan.notes, *caps.notes],
            "consent_reason": reason,
            "uploads": "the model" if not task else f"a reference to its {cls.display_name} task",
        },
        cloud_upload_consented=consented,
        queued_at=now if consented else None,
    )
    db.add(job)
    db.commit()
    sent = (
        f"version {version.version_number} of the model"
        if not task
        else f"a request to re-texture its {cls.display_name} model"
    )
    add_event(
        db,
        job.id,
        JobEventType.STATUS,
        f"Queued for {cls.display_name}."
        if consented
        else f"Waiting for your permission to send {sent} to {cls.display_name}. Nothing has "
        "been sent.",
    )
    return job


def latest_retexture(db: Session, model_id: uuid.UUID) -> GenerationJob | None:
    return db.scalar(
        select(GenerationJob)
        .where(
            GenerationJob.kind == JobKind.RETEXTURE,
            GenerationJob.params["model_id"].as_string() == str(model_id),
        )
        .order_by(GenerationJob.created_at.desc())
        .limit(1)
    )
