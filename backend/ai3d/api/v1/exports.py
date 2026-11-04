import uuid

from fastapi import APIRouter, Response, status
from fastapi.responses import FileResponse, StreamingResponse

from ai3d.api.deps import DbDep, SettingsDep, StorageDep
from ai3d.core.errors import NotFoundError
from ai3d.schemas.export import ExportCreate, ExportFormatList, ExportList, ExportRead
from ai3d.services import exports as export_service

router = APIRouter(tags=["exports"])


@router.get("/exports/formats", response_model=ExportFormatList, summary="Export formats")
def list_formats() -> ExportFormatList:
    return ExportFormatList(items=export_service.list_formats())


@router.post(
    "/model-versions/{version_id}/exports",
    response_model=ExportRead,
    status_code=status.HTTP_201_CREATED,
    summary="Export a version to another format",
    description="Converts the version, re-opens the file with an independent reader and "
    "compares triangle count, size and texture with the source. A file that does not match "
    "is reported as failed and not stored.",
)
def create_export(
    db: DbDep,
    storage: StorageDep,
    settings: SettingsDep,
    version_id: uuid.UUID,
    body: ExportCreate,
) -> ExportRead:
    export = export_service.create_export(db, storage, settings, version_id, body.format)
    return export_service.to_read(db, export)


@router.get(
    "/model-versions/{version_id}/exports",
    response_model=ExportList,
    summary="Exports of a version, newest first",
)
def list_exports(db: DbDep, version_id: uuid.UUID) -> ExportList:
    return ExportList(
        items=[export_service.to_read(db, e) for e in export_service.list_exports(db, version_id)]
    )


@router.get(
    "/exports/{export_id}/file",
    summary="Download an exported file",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}, "description": "File"}},
)
def get_export_file(db: DbDep, storage: StorageDep, export_id: uuid.UUID) -> Response:
    _, asset, filename = export_service.export_file(db, export_id)
    if not storage.exists(asset.storage_key):
        raise NotFoundError("File not found in storage.")
    path = storage.local_path(asset.storage_key)
    if path is not None:
        return FileResponse(path, media_type=asset.mime_type, filename=filename)
    return StreamingResponse(
        storage.open(asset.storage_key),
        media_type=asset.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/exports/{export_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an exported file (the model version is not affected)",
)
def delete_export(db: DbDep, storage: StorageDep, export_id: uuid.UUID) -> Response:
    export_service.delete_export(db, storage, export_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
