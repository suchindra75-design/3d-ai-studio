"""CGI scenes made from a prompt (D76): creating, rendering again, reading, deleting.

The job itself is ``workers/scenes.py``. A scene needs the user's GPU server (it
plans the scene and makes the objects) and Blender on this computer (it renders);
both are checked before a job is created. The prompt goes only to the GPU server,
and only with the user's explicit agreement (``send_to_gpu_server``).
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai3d.core.errors import ConflictError, InvalidInputError, NotFoundError
from ai3d.core.timeutil import utcnow
from ai3d.models import GenerationJob, Model3D, Scene
from ai3d.models.enums import JobEventType, JobKind, JobStatus, ProcessingMode, QualityMode
from ai3d.processing.scene_plan import ScenePlan, with_choices
from ai3d.providers.gpu_server import GpuServerProvider
from ai3d.schemas.scene import (
    SceneCreate,
    SceneFileRead,
    SceneObjectRead,
    SceneRead,
    SceneRenderAgain,
    SceneRenderRead,
)
from ai3d.services import jobs as job_service
from ai3d.services.projects import get_project
from ai3d.services.providers import ProviderManager
from ai3d.storage.base import StorageBackend
from ai3d.storage.keys import project_prefix
from ai3d_engine.blender import check_blender

log = logging.getLogger(__name__)


def _choices(body: SceneCreate | SceneRenderAgain, quality: QualityMode) -> dict[str, Any]:
    return {
        "seconds": body.seconds,
        "quality_mode": quality.value,
        "shot": body.shot,
        "angle": body.angle,
        "lighting": body.lighting,
        "ground": body.ground,
        "style": body.style,
    }


def _need_blender() -> None:
    problem = check_blender()
    if problem is not None:
        raise ConflictError(
            f"Scenes are rendered with Blender on this computer. {problem}",
            code="blender_missing",
        )


def _new_job(
    db: Session,
    scene: Scene,
    *,
    quality: QualityMode,
    render_only: bool,
    provider_config_id: uuid.UUID | None,
    idempotency_key: str | None,
    parent_job_id: uuid.UUID | None = None,
) -> GenerationJob:
    refine = None
    summary: list[str] = ["Rendered with Blender on this computer (video, still, .blend, GLB)"]
    if not render_only:
        plan = GpuServerProvider.plan(JobKind.IMAGE_TO_3D, quality)
        refine = plan.settings["refine"]
        summary = [
            "Scene planned on your GPU server (Qwen3-4B-Instruct)",
            "A reference picture of each object painted there (FLUX.1-schnell)",
            "Each object shaped there by TRELLIS, then cleaned up and textured here",
            *summary,
        ]
    return GenerationJob(
        project_id=scene.project_id,
        kind=JobKind.SCENE,
        status=JobStatus.QUEUED,
        quality_mode=quality,
        processing_mode=ProcessingMode.LOCAL if render_only else ProcessingMode.CLOUD,
        provider_key=None if render_only else GpuServerProvider.key,
        provider_config_id=provider_config_id,
        params={
            "scene_id": str(scene.id),
            "render_only": render_only,
            "provider_settings": {"refine": refine} if refine else {},
            "provider_summary": summary,
            "warnings": [],
            "consent_reason": None,
        },
        cloud_upload_consented=not render_only,
        idempotency_key=idempotency_key,
        parent_job_id=parent_job_id,
        queued_at=utcnow(),
    )


def _by_key(db: Session, key: str, project_id: uuid.UUID) -> Scene | None:
    existing = db.scalar(
        select(Scene)
        .join(GenerationJob, Scene.job_id == GenerationJob.id)
        .where(GenerationJob.idempotency_key == key)
    )
    if existing is not None and existing.project_id != project_id:
        raise ConflictError("This Idempotency-Key was already used for another project.")
    return existing


def create_scene(
    db: Session,
    manager: ProviderManager,
    project_id: uuid.UUID,
    body: SceneCreate,
    *,
    idempotency_key: str | None = None,
) -> tuple[Scene, bool]:
    """A new scene and its job. Returns ``(scene, created)``; a repeated key returns
    the original scene."""
    project = get_project(db, project_id)
    if idempotency_key and (existing := _by_key(db, idempotency_key, project_id)) is not None:
        return existing, False
    if not body.send_to_gpu_server:
        raise InvalidInputError(
            "Agree to send the prompt to your GPU server: it plans the scene and makes the "
            "objects there.",
            code="consent_required",
        )
    if not manager.is_configured(db, GpuServerProvider.key):
        raise ConflictError(
            "Scenes are made on your own GPU server. Set its address and access token in "
            "Settings → AI providers first (see docs/GPU_SERVER.md).",
            code="gpu_server_not_configured",
        )
    _need_blender()
    quality = body.quality_mode or project.default_quality_mode
    credential = manager.credential(db, GpuServerProvider.key)
    scene = Scene(
        id=uuid.uuid4(),
        project_id=project_id,
        title=body.prompt.strip()[:120],
        prompt=body.prompt.strip(),
        plan={},
        settings=_choices(body, quality),
        files={},
    )
    job = _new_job(
        db,
        scene,
        quality=quality,
        render_only=False,
        provider_config_id=credential.config_id,
        idempotency_key=idempotency_key,
    )
    db.add(job)
    db.flush()
    scene.job_id = job.id
    db.add(scene)
    try:
        db.commit()
    except IntegrityError:  # the same Idempotency-Key at the same moment
        db.rollback()
        if idempotency_key and (existing := _by_key(db, idempotency_key, project_id)):
            return existing, False
        raise
    job_service.add_event(
        db,
        job.id,
        JobEventType.STATUS,
        "Queued. The prompt goes to your GPU server only; nothing else is contacted.",
    )
    log.info("scene job created", extra={"job_id": str(job.id), "scene_id": str(scene.id)})
    return scene, True


def get_scene(db: Session, scene_id: uuid.UUID) -> Scene:
    scene = db.get(Scene, scene_id)
    if scene is None:
        raise NotFoundError("Scene not found.", details={"scene_id": str(scene_id)})
    return scene


def _ready_plan(scene: Scene) -> ScenePlan:
    if not scene.plan.get("objects"):
        raise ConflictError("This scene has no plan yet.", code="scene_not_ready")
    plan = ScenePlan.model_validate(scene.plan)
    if any(o.model_version_id is None for o in plan.objects):
        raise ConflictError(
            "Not all of this scene's objects were made yet; retry its job first.",
            code="scene_not_ready",
        )
    return plan


def render_again(
    db: Session, scene_id: uuid.UUID, body: SceneRenderAgain, *, idempotency_key: str | None
) -> Scene:
    """A new scene with the same objects and plan, rendered with other choices."""
    source = get_scene(db, scene_id)
    plan = _ready_plan(source)
    _need_blender()
    project = get_project(db, source.project_id)
    quality = body.quality_mode or project.default_quality_mode
    choices = _choices(body, quality)
    # Keep where the objects stood; the new choices replace the plan's.
    replanned = with_choices(plan, {k: v for k, v in choices.items() if isinstance(v, str)})
    scene = Scene(
        id=uuid.uuid4(),
        project_id=source.project_id,
        source_scene_id=source.id,
        title=source.title,
        prompt=source.prompt,
        plan=replanned.model_dump(mode="json"),
        settings=choices,
        files={},
    )
    job = _new_job(
        db,
        scene,
        quality=quality,
        render_only=True,
        provider_config_id=None,
        idempotency_key=idempotency_key,
    )
    db.add(job)
    db.flush()
    scene.job_id = job.id
    db.add(scene)
    db.commit()
    job_service.add_event(
        db, job.id, JobEventType.STATUS, "Queued to render on this computer (nothing is sent)."
    )
    return scene


def retry_scene(
    db: Session, original: GenerationJob, *, idempotency_key: str | None
) -> GenerationJob:
    """Run a failed or cancelled scene job again: objects already made are kept."""
    scene = db.get(Scene, uuid.UUID(original.params.get("scene_id", "")))
    if scene is None:
        raise ConflictError("The scene of this job was deleted.")
    job = _new_job(
        db,
        scene,
        quality=original.quality_mode,
        render_only=bool(original.params.get("render_only")),
        provider_config_id=original.provider_config_id,
        idempotency_key=idempotency_key,
        parent_job_id=original.id,
    )
    db.add(job)
    db.flush()
    scene.job_id = job.id
    db.commit()
    job_service.add_event(
        db, job.id, JobEventType.STATUS, "Queued again; objects already made are kept."
    )
    return job


def list_scenes(db: Session, project_id: uuid.UUID) -> list[Scene]:
    get_project(db, project_id)
    return list(
        db.scalars(
            select(Scene).where(Scene.project_id == project_id).order_by(Scene.created_at.desc())
        )
    )


def delete_scene(db: Session, storage: StorageBackend, scene_id: uuid.UUID) -> None:
    """Delete the scene and its rendered files; its objects stay as models."""
    scene = get_scene(db, scene_id)
    if scene.job_id is not None:
        job = db.get(GenerationJob, scene.job_id)
        if job is not None and not job.status.is_terminal:
            raise ConflictError("Cancel the scene's job before deleting it.")
    project_id = scene.project_id
    db.delete(scene)
    db.commit()
    try:
        storage.delete_prefix(f"{project_prefix(project_id)}scenes/{scene_id}/")
    except OSError:
        log.exception("failed to delete scene files", extra={"scene_id": str(scene_id)})


def scene_file(db: Session, scene_id: uuid.UUID, name: str) -> tuple[Scene, dict[str, Any]]:
    scene = get_scene(db, scene_id)
    entry = scene.files.get(name)
    if not isinstance(entry, dict):
        raise NotFoundError("This scene has no such file.")
    return scene, entry


def _status(job: GenerationJob | None, scene: Scene) -> str:
    if job is None:
        return "ready" if scene.files.get("video") else "failed"
    if job.status == JobStatus.SUCCEEDED:
        return "ready"
    if job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
        return job.status.value
    return "making"


def to_read(db: Session, manager: ProviderManager, scene: Scene) -> SceneRead:
    job = db.get(GenerationJob, scene.job_id) if scene.job_id else None
    plan = ScenePlan.model_validate(scene.plan) if scene.plan.get("objects") else None
    settings = scene.settings

    def pick(field: str, default: str) -> Any:
        """The plan's value; before there is a plan, the user's choice or the default."""
        if plan is not None:
            return getattr(plan, field)
        chosen = settings.get(field)
        return chosen if chosen not in {None, "auto"} else default

    existing_models = set()
    if plan is not None:
        ids = [uuid.UUID(o.model_id) for o in plan.objects if o.model_id]
        if ids:
            existing_models = {
                str(i) for i in db.scalars(select(Model3D.id).where(Model3D.id.in_(ids)))
            }
    objects = [
        SceneObjectRead(
            name=o.name,
            description=o.description,
            size_m=o.size_m,
            position_m=o.position_m,
            elevation_m=o.elevation_m,
            facing_deg=o.facing_deg,
            motion=o.motion,
            model_id=uuid.UUID(o.model_id) if o.model_id in existing_models else None,
            model_version_id=uuid.UUID(o.model_version_id)
            if o.model_id in existing_models and o.model_version_id
            else None,
        )
        for o in (plan.objects if plan else [])
    ]
    files = [
        SceneFileRead(
            name=name,
            filename=entry["filename"],
            mime_type=entry["mime_type"],
            size_bytes=entry["size_bytes"],
            url=f"/api/v1/scenes/{scene.id}/files/{name}",
        )
        for name in ("video", "still", "blend", "glb")
        if isinstance(entry := scene.files.get(name), dict)
    ]
    render = scene.render
    return SceneRead(
        id=scene.id,
        project_id=scene.project_id,
        title=scene.title,
        prompt=scene.prompt,
        status=_status(job, scene),
        job=job_service.to_read(db, manager, job_service.get_job(db, job.id)) if job else None,
        source_scene_id=scene.source_scene_id,
        style=pick("style", "cinematic"),
        lighting=pick("lighting", "studio"),
        ground=pick("ground", "studio"),
        shot=pick("shot", "orbit"),
        angle=pick("angle", "eye"),
        lens_mm=plan.lens_mm if plan else 35.0,
        seconds=float(settings.get("seconds") or 6.0),
        quality_mode=QualityMode(settings.get("quality_mode") or QualityMode.FAST.value),
        objects=objects,
        files=files,
        render=SceneRenderRead(
            frames=int(render.get("frames", 0)),
            fps=int(render.get("fps", 24)),
            width=int(render.get("width", 0)),
            height=int(render.get("height", 0)),
            engines=dict(render.get("engines") or {}),
            seconds={k: float(v) for k, v in (render.get("seconds") or {}).items()},
        )
        if render
        else None,
        created_at=scene.created_at,
        updated_at=scene.updated_at,
    )


def check_ready(db: Session, manager: ProviderManager) -> dict[str, Any]:
    """What is missing before scenes can be made (shown before the prompt box)."""
    problems: list[str] = []
    if not manager.is_configured(db, GpuServerProvider.key):
        problems.append(
            "Set up your GPU server in Settings → AI providers (it plans the scene and makes "
            "the objects)."
        )
    blender = check_blender()
    if blender is not None:
        problems.append(f"Install Blender to render scenes: {blender}")
    return {"ready": not problems, "problems": problems}
