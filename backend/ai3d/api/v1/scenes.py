import uuid
from typing import Literal

from fastapi import APIRouter, Response, status
from fastapi.responses import FileResponse, StreamingResponse

from ai3d.api.deps import DbDep, ProvidersDep, RunnerDep, StorageDep
from ai3d.api.v1.jobs import IdempotencyKey
from ai3d.core.errors import NotFoundError
from ai3d.schemas.scene import (
    SceneCreate,
    SceneList,
    SceneRead,
    SceneReadiness,
    SceneRenderAgain,
)
from ai3d.services import scenes as scene_service

router = APIRouter(tags=["scenes"])

_IMMUTABLE = "private, max-age=31536000, immutable"


@router.get(
    "/scenes/readiness",
    response_model=SceneReadiness,
    summary="Whether scenes can be made here (GPU server set up, Blender installed)",
)
def readiness(db: DbDep, manager: ProvidersDep) -> SceneReadiness:
    return SceneReadiness.model_validate(scene_service.check_ready(db, manager))


@router.get(
    "/projects/{project_id}/scenes", response_model=SceneList, summary="List a project's scenes"
)
def list_scenes(db: DbDep, manager: ProvidersDep, project_id: uuid.UUID) -> SceneList:
    return SceneList(
        items=[
            scene_service.to_read(db, manager, s) for s in scene_service.list_scenes(db, project_id)
        ]
    )


@router.post(
    "/projects/{project_id}/scenes",
    response_model=SceneRead,
    status_code=status.HTTP_201_CREATED,
    summary="Make a CGI scene from a prompt",
    description="Plans the scene and makes its objects on your GPU server (the prompt is "
    "sent there only with `send_to_gpu_server: true`), then renders a video, a still, a "
    "Blender file and an animated GLB with Blender on this computer.",
)
def create_scene(
    *,
    db: DbDep,
    manager: ProvidersDep,
    runner: RunnerDep,
    response: Response,
    project_id: uuid.UUID,
    body: SceneCreate,
    idempotency_key: IdempotencyKey = None,
) -> SceneRead:
    scene, created = scene_service.create_scene(
        db, manager, project_id, body, idempotency_key=idempotency_key
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    runner.wake()
    return scene_service.to_read(db, manager, scene)


@router.get("/scenes/{scene_id}", response_model=SceneRead, summary="Get a scene")
def get_scene(db: DbDep, manager: ProvidersDep, scene_id: uuid.UUID) -> SceneRead:
    return scene_service.to_read(db, manager, scene_service.get_scene(db, scene_id))


@router.post(
    "/scenes/{scene_id}/render",
    response_model=SceneRead,
    status_code=status.HTTP_201_CREATED,
    summary="Render the same objects again with other choices (on this computer)",
)
def render_again(
    *,
    db: DbDep,
    manager: ProvidersDep,
    runner: RunnerDep,
    scene_id: uuid.UUID,
    body: SceneRenderAgain,
    idempotency_key: IdempotencyKey = None,
) -> SceneRead:
    scene = scene_service.render_again(db, scene_id, body, idempotency_key=idempotency_key)
    runner.wake()
    return scene_service.to_read(db, manager, scene)


@router.delete(
    "/scenes/{scene_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a scene and its rendered files (its objects stay as models)",
)
def delete_scene(db: DbDep, storage: StorageDep, scene_id: uuid.UUID) -> Response:
    scene_service.delete_scene(db, storage, scene_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/scenes/{scene_id}/files/{name}",
    response_class=Response,
    summary="A rendered file: the video (MP4), the still (PNG), the Blender file or the GLB",
)
def get_file(
    db: DbDep,
    storage: StorageDep,
    scene_id: uuid.UUID,
    name: Literal["video", "still", "blend", "glb"],
    download: bool = False,
) -> Response:
    _scene, entry = scene_service.scene_file(db, scene_id, name)
    key = str(entry["key"])
    if not storage.exists(key):
        raise NotFoundError("File not found in storage.")
    headers = {"Cache-Control": _IMMUTABLE, "ETag": f'"{entry["sha256"]}"'}
    path = storage.local_path(key)
    if path is not None:  # supports Range requests (seeking in the video)
        return FileResponse(
            path,
            media_type=str(entry["mime_type"]),
            headers=headers,
            filename=str(entry["filename"]),
            content_disposition_type="attachment" if download else "inline",
        )
    disposition = "attachment" if download else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{entry["filename"]}"'
    return StreamingResponse(storage.open(key), media_type=str(entry["mime_type"]), headers=headers)
