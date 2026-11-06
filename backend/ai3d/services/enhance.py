"""Enhance an existing model: plan the processing steps and queue the job.

The source version is never modified; the result is a new ``enhanced``
version (see workers/enhance.py).
"""

import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai3d.core.errors import ConflictError, InvalidInputError
from ai3d.core.timeutil import utcnow
from ai3d.models import GenerationJob, ModelVersion
from ai3d.models.enums import JobEventType, JobKind, JobStatus, ProcessingMode
from ai3d.processing.geometry import (
    BAKE_MAX_TEXTURE,
    PROFILES,
    REMESH_MAX_P95,
    UPSCALE_DETAIL,
)
from ai3d.schemas.enhance import EnhancePlanRead, EnhanceRequest, EnhanceStepPlan
from ai3d.services.jobs import add_event
from ai3d.services.models import QUALITY_LABELS, get_model, get_version
from ai3d_engine.blender import check_blender
from ai3d_engine.local_ai import engine as local_ai_engine


def _surface_steps(body: EnhanceRequest, max_texture: int, step: Callable[..., None]) -> None:
    """The optional Surface steps: smoothing, retopology, baked maps, AI upscaling."""
    if body.smoothing:
        step(
            "smoothing",
            "Smoothing",
            f"{body.smoothing} passes, "
            + (
                "keeping sharp edges and corners"
                if body.smoothing_method == "sharp"
                else "rounding sharp edges (Taubin)"
            )
            + "; keeps the volume. The report says how far the surface moved.",
        )
    remesh_problem = check_blender() if body.remesh else None
    if body.remesh:
        method = (
            f"QuadriFlow, about {body.remesh.target_faces:,} quads (an even layout)"
            if body.remesh.method == "quadriflow"
            else f"voxels, {body.remesh.resolution} across (a uniform, closed surface)"
        )
        step(
            "retopology",
            "Retopology" if body.remesh.method == "quadriflow" else "Remesh",
            remesh_problem
            or f"Rebuilds the surface in Blender ({method}). Kept only if it stays within "
            f"{REMESH_MAX_P95:.0%} of the model's size of the original; colours are carried "
            "over and textures baked onto new UVs. Can take minutes.",
            available=remesh_problem is None,
        )
        if body.remesh.maps:
            names = {"normal": "normal map", "occlusion": "ambient occlusion"}
            step(
                "maps",
                "Normal / occlusion maps",
                remesh_problem
                or "Bakes " + " and ".join(names[m] for m in body.remesh.maps) + " in Blender.",
                available=remesh_problem is None,
            )
    if body.upscale_textures:
        problem = local_ai_engine.upscaler_problem()
        step(
            "upscale",
            f"AI texture upscaling (×{body.upscale_textures})",
            problem
            or "Real-ESRGAN on this computer enlarges base-colour textures (up to "
            f"{max_texture} px). {UPSCALE_DETAIL}",
            available=problem is None,
        )


def build_plan(body: EnhanceRequest) -> EnhancePlanRead:
    profile = PROFILES[body.quality_mode]
    steps: list[EnhanceStepPlan] = []

    def step(code: str, title: str, detail: str, *, available: bool = True) -> None:
        steps.append(
            EnhanceStepPlan(
                code=code,
                title=title,
                status="will_run" if available else "unavailable",
                detail=detail,
            )
        )

    if body.remove_ground:
        step(
            "ground",
            "Remove ground / table",
            "Finds a large flat surface under the object and removes it with anything loose.",
        )
    step("cleanup", "Clean-up", "Removes zero-area and duplicate triangles.")
    step(
        "small_parts",
        "Remove loose fragments",
        f"Removes pieces smaller than {profile.min_part_fraction:.1%} of the model.",
    )
    if profile.fix_normals:
        step("normals", "Consistent normals", "Flips triangles that face the wrong way.")
    if profile.fill_holes:
        step("holes", "Fill holes", "Closes small holes on untextured, uncoloured meshes.")
    _surface_steps(body, profile.max_texture, step)
    step("textures", "Texture size", f"Downscales textures larger than {profile.max_texture} px.")
    blender_problem = check_blender() if profile.report_tools else None
    if profile.decimate_to is not None:
        step(
            "decimate",
            f"Reduce to {profile.decimate_to:,} triangles",
            "Quadric decimation. Textured models are reduced in Blender with texture baking."
            if profile.report_tools and blender_problem is None
            else "Quadric decimation on untextured models (textured models are kept as they are).",
        )
    if profile.report_tools and body.remesh is None:
        if blender_problem is None:
            step(
                "baking",
                "Texture baking",
                f"Only for textured models over {profile.decimate_to or 0:,} triangles: Blender "
                "reduces a copy, unwraps new UVs and bakes the original's base colour onto it "
                f"(up to {BAKE_MAX_TEXTURE} px, on the CPU; this can take several minutes).",
            )
        else:
            step("baking", "Texture baking", blender_problem, available=False)
    if body.scale is not None:
        step(
            "scale",
            "Real-world scale",
            f"Scales the model so its {body.scale.axis} extent is {body.scale.length_mm:g} mm.",
        )
    notes = [
        "The source version is kept unchanged; the result is saved as a new version.",
        "Steps only change what they find; the report lists what was actually done.",
    ]
    if body.scale is None:
        notes.append(
            "Without a measurement the size stays unknown. Enter one real length (e.g. the "
            "height in mm) to give the model a real-world scale."
        )
    return EnhancePlanRead(quality_mode=body.quality_mode, steps=steps, notes=notes)


def _resolve_source(db: Session, model_id: uuid.UUID, body: EnhanceRequest) -> ModelVersion:
    model = get_model(db, model_id)
    version_id = body.source_version_id or model.current_version_id
    if version_id is None:
        raise InvalidInputError("This model has no version to enhance.")
    version = get_version(db, version_id)
    if version.model_id != model.id:
        raise InvalidInputError("That version belongs to a different model.")
    return version


def retry_enhance(
    db: Session, original: GenerationJob, *, idempotency_key: str | None = None
) -> GenerationJob:
    params = original.params or {}
    body = EnhanceRequest(
        quality_mode=original.quality_mode,
        source_version_id=original.source_model_version_id,
        remove_ground=bool(params.get("remove_ground")),
        scale=params.get("scale"),
        smoothing=int(params.get("smoothing") or 0),
        # Jobs from before the choice existed used Taubin.
        smoothing_method=params.get("smoothing_method") or "taubin",
        remesh=params.get("remesh"),
        upscale_textures=params.get("upscale_textures"),
    )
    return create_enhance_job(
        db,
        uuid.UUID(params["model_id"]),
        body,
        idempotency_key=idempotency_key,
        parent_job_id=original.id,
    )


def create_enhance_job(
    db: Session,
    model_id: uuid.UUID,
    body: EnhanceRequest,
    *,
    idempotency_key: str | None = None,
    parent_job_id: uuid.UUID | None = None,
) -> GenerationJob:
    if idempotency_key:
        existing = db.scalar(
            select(GenerationJob).where(GenerationJob.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.kind != JobKind.ENHANCE or existing.params.get("model_id") != str(model_id):
                raise ConflictError("This Idempotency-Key was already used for another request.")
            return existing
    source = _resolve_source(db, model_id, body)
    model = get_model(db, model_id)
    plan = build_plan(body)
    job = GenerationJob(
        project_id=model.project_id,
        kind=JobKind.ENHANCE,
        status=JobStatus.QUEUED,
        quality_mode=body.quality_mode,
        processing_mode=ProcessingMode.LOCAL,
        source_model_version_id=source.id,
        params={
            "model_id": str(model.id),
            "source_version_number": source.version_number,
            "remove_ground": body.remove_ground,
            "scale": body.scale.model_dump() if body.scale else None,
            "smoothing": body.smoothing,
            "smoothing_method": body.smoothing_method,
            "remesh": body.remesh.model_dump() if body.remesh else None,
            "upscale_textures": body.upscale_textures,
            "provider_summary": [s.title for s in plan.steps if s.status == "will_run"],
            "warnings": [],
        },
        idempotency_key=idempotency_key,
        parent_job_id=parent_job_id,
        queued_at=utcnow(),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ConflictError("This Idempotency-Key was already used.") from None
    add_event(
        db,
        job.id,
        JobEventType.STATUS,
        f"Queued: enhance version {source.version_number} of “{model.name}” "
        f"({QUALITY_LABELS[body.quality_mode]}). Runs on this computer.",
    )
    return job
