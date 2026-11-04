import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Header, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from ai3d.api.deps import DbDep, ProvidersDep, RunnerDep, SettingsDep, StorageDep
from ai3d.core.errors import NotFoundError
from ai3d.models.enums import AssetKind
from ai3d.schemas.enhance import EnhancePlanRead, EnhanceRequest
from ai3d.schemas.job import JobRead
from ai3d.schemas.model3d import (
    ModelList,
    ModelRead,
    ModelUpdate,
    ModelVersionList,
    ModelVersionRead,
)
from ai3d.services import enhance as enhance_service
from ai3d.services import jobs as job_service
from ai3d.services import models as model_service
from ai3d.services import transfer as transfer_service

router = APIRouter(tags=["models"])

_IMMUTABLE = "private, max-age=31536000, immutable"


@router.get(
    "/projects/{project_id}/models", response_model=ModelList, summary="List a project's 3D models"
)
def list_models(db: DbDep, providers: ProvidersDep, project_id: uuid.UUID) -> ModelList:
    models = model_service.list_models(db, project_id)
    return ModelList(
        items=[model_service.model_to_read(db, providers.registry, m) for m in models],
        total=len(models),
    )


@router.post(
    "/projects/{project_id}/models/import",
    response_model=ModelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Import an existing 3D file as a new model",
    description="GLB, OBJ, STL, PLY or 3MF, or a ZIP with an OBJ (plus MTL and textures) "
    "or a glTF (plus .bin and images). Converted to GLB, re-opened and checked; a file "
    "that fails the checks is rejected (422). The version's origin is recorded as "
    "imported and its accuracy as unknown.",
)
def import_model(
    *,
    db: DbDep,
    storage: StorageDep,
    settings: SettingsDep,
    providers: ProvidersDep,
    project_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="The 3D file or ZIP")],
    units: Annotated[
        Literal["m", "cm", "mm", "in"] | None,
        Form(description="Units of STL/OBJ/PLY/3MF (glTF is always metres)"),
    ] = None,
    up_axis: Annotated[Literal["y", "z"] | None, Form(description="Which axis is up")] = None,
    real_scale: Annotated[
        bool, Form(description="The file's size is the real object's size")
    ] = False,
    name: Annotated[str | None, Form(max_length=200)] = None,
) -> ModelRead:
    model = transfer_service.import_model(
        db,
        storage,
        settings,
        project_id,
        filename=file.filename or "model",
        stream=file.file,
        units=units,
        up=up_axis,
        real_scale=real_scale,
        name=name,
    )
    return model_service.model_to_read(db, providers.registry, model)


@router.get("/models/{model_id}", response_model=ModelRead, summary="Get a 3D model")
def get_model(db: DbDep, providers: ProvidersDep, model_id: uuid.UUID) -> ModelRead:
    return model_service.model_to_read(
        db, providers.registry, model_service.get_model(db, model_id)
    )


@router.patch("/models/{model_id}", response_model=ModelRead, summary="Rename or switch version")
def update_model(
    db: DbDep, providers: ProvidersDep, model_id: uuid.UUID, body: ModelUpdate
) -> ModelRead:
    model = model_service.update_model(db, model_id, body)
    return model_service.model_to_read(db, providers.registry, model)


@router.get(
    "/models/{model_id}/versions",
    response_model=ModelVersionList,
    summary="Version history, newest first",
)
def list_versions(db: DbDep, providers: ProvidersDep, model_id: uuid.UUID) -> ModelVersionList:
    versions = model_service.list_versions(db, model_id)
    model = model_service.get_model(db, model_id)
    return ModelVersionList(
        items=[model_service.version_to_read(db, providers.registry, v) for v in versions],
        current_version_id=model.current_version_id,
    )


@router.post(
    "/models/{model_id}/versions",
    response_model=ModelVersionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Save an edited GLB as a new version",
    description="The parent version is kept unchanged. The file is re-opened and checked "
    "like provider output; a file that fails the checks is rejected (422) and nothing is "
    "stored. The new version becomes the model's current version.",
)
def create_version(
    *,
    db: DbDep,
    storage: StorageDep,
    settings: SettingsDep,
    providers: ProvidersDep,
    model_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="GLB file")],
    parent_version_id: Annotated[uuid.UUID, Form()],
    changes: Annotated[list[str] | None, Form(description="Human-readable edit list")] = None,
) -> ModelVersionRead:
    version = model_service.add_edited_version(
        db,
        storage,
        settings,
        model_id=model_id,
        parent_version_id=parent_version_id,
        stream=file.file,
        changes=changes or [],
    )
    return model_service.version_to_read(db, providers.registry, version)


@router.delete(
    "/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a model, all its versions and files",
)
def delete_model(db: DbDep, storage: StorageDep, model_id: uuid.UUID) -> Response:
    model_service.delete_model(db, storage, model_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _serve(
    db: DbDep, storage: StorageDep, version_id: uuid.UUID, kind: AssetKind, attachment: bool
) -> Response:
    version, asset = model_service.version_file(db, version_id, kind)
    if not storage.exists(asset.storage_key):
        raise NotFoundError("File not found in storage.")
    headers = {"Cache-Control": _IMMUTABLE, "ETag": f'"{asset.sha256}"'}
    filename = None
    if attachment:
        model = model_service.get_model(db, version.model_id)
        filename = model_service.download_filename(model, version, "glb")
    path = storage.local_path(asset.storage_key)
    if path is not None:
        return FileResponse(
            path,
            media_type=asset.mime_type,
            headers=headers,
            filename=filename,
            content_disposition_type="attachment" if attachment else "inline",
        )
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return StreamingResponse(
        storage.open(asset.storage_key), media_type=asset.mime_type, headers=headers
    )


@router.get(
    "/model-versions/{version_id}/file",
    summary="Download the version's 3D file (GLB)",
    response_class=Response,
    responses={200: {"content": {"model/gltf-binary": {}}, "description": "GLB file"}},
)
def get_version_file(db: DbDep, storage: StorageDep, version_id: uuid.UUID) -> Response:
    return _serve(db, storage, version_id, AssetKind.MESH, attachment=True)


@router.get(
    "/model-versions/{version_id}/preview",
    summary="Preview image rendered by the provider (WebP)",
    response_class=Response,
    responses={200: {"content": {"image/webp": {}}, "description": "Preview"}},
)
def get_version_preview(db: DbDep, storage: StorageDep, version_id: uuid.UUID) -> Response:
    return _serve(db, storage, version_id, AssetKind.PREVIEW_IMAGE, attachment=False)


@router.post(
    "/models/{model_id}/enhance/plan",
    response_model=EnhancePlanRead,
    summary="Preview the processing steps an enhancement would run",
)
def plan_enhance(model_id: uuid.UUID, body: EnhanceRequest, db: DbDep) -> EnhancePlanRead:
    model_service.get_model(db, model_id)
    return enhance_service.build_plan(body)


@router.post(
    "/models/{model_id}/enhance",
    response_model=JobRead,
    status_code=status.HTTP_201_CREATED,
    summary="Enhance a version into a new version (background job, on this machine)",
    description="Runs the quality profile's geometry and texture processing. The source "
    "version is never changed; the result is a new `enhanced` version.",
)
def enhance(
    *,
    model_id: uuid.UUID,
    body: EnhanceRequest,
    db: DbDep,
    providers: ProvidersDep,
    runner: RunnerDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=255)] = None,
) -> JobRead:
    job = enhance_service.create_enhance_job(db, model_id, body, idempotency_key=idempotency_key)
    runner.wake()
    return job_service.to_read(db, providers, job_service.get_job(db, job.id))
