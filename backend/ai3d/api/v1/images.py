import uuid
from typing import Annotated

from fastapi import APIRouter, File, Response, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from ai3d.api.deps import DbDep, SettingsDep, StorageDep
from ai3d.core.errors import InvalidInputError, NotFoundError
from ai3d.models import Image
from ai3d.schemas.image import ImageList, ImageRead, UploadResponse
from ai3d.services import images as image_service
from ai3d.storage import StorageBackend
from ai3d.storage.keys import image_cutout_key

router = APIRouter(tags=["images"])

MAX_FILES_PER_REQUEST = 50
# Keys embed the image id and stored bytes never change.
_IMMUTABLE = "private, max-age=31536000, immutable"


@router.post(
    "/projects/{project_id}/images",
    response_model=UploadResponse,
    summary="Upload one or more photos (JPEG, PNG, WebP)",
    description="Each file is validated and analysed independently; one bad file does not "
    "fail the request. Re-uploading identical bytes returns the existing image "
    "(status `duplicate`).",
)
def upload_images(
    db: DbDep,
    storage: StorageDep,
    settings: SettingsDep,
    project_id: uuid.UUID,
    files: Annotated[list[UploadFile], File(description="Image files")],
) -> UploadResponse:
    if len(files) > MAX_FILES_PER_REQUEST:
        raise InvalidInputError(
            f"Upload at most {MAX_FILES_PER_REQUEST} files per request.",
            details={"max_files": MAX_FILES_PER_REQUEST},
        )
    incoming = [image_service.IncomingFile(f.filename or "image", f.file) for f in files]
    return image_service.upload_images(db, storage, settings, project_id, incoming)


@router.get(
    "/projects/{project_id}/images", response_model=ImageList, summary="List a project's photos"
)
def list_images(db: DbDep, project_id: uuid.UUID) -> ImageList:
    images = image_service.list_images(db, project_id)
    return ImageList(items=[ImageRead.model_validate(i) for i in images], total=len(images))


@router.get("/images/{image_id}", response_model=ImageRead, summary="Get photo metadata")
def get_image(db: DbDep, image_id: uuid.UUID) -> Image:
    return image_service.get_image(db, image_id)


def _serve(
    storage: StorageBackend,
    key: str | None,
    media_type: str,
    etag: str,
    filename: str | None = None,
) -> Response:
    if not key or not storage.exists(key):
        raise NotFoundError("File not found in storage.")
    headers = {"Cache-Control": _IMMUTABLE, "ETag": f'"{etag}"'}
    path = storage.local_path(key)
    if path is not None:
        return FileResponse(
            path,
            media_type=media_type,
            headers=headers,
            filename=filename,
            content_disposition_type="inline",
        )
    return StreamingResponse(storage.open(key), media_type=media_type, headers=headers)


@router.get(
    "/images/{image_id}/file",
    summary="Download the original, unmodified photo",
    response_class=Response,
    responses={200: {"content": {"image/*": {}}, "description": "Original bytes"}},
)
def get_image_file(db: DbDep, storage: StorageDep, image_id: uuid.UUID) -> Response:
    image = image_service.get_image(db, image_id)
    return _serve(
        storage, image.storage_key, image.mime_type, image.sha256, image.original_filename
    )


@router.get(
    "/images/{image_id}/thumbnail",
    summary="WebP thumbnail (EXIF orientation applied)",
    response_class=Response,
    responses={200: {"content": {"image/webp": {}}, "description": "Thumbnail"}},
)
def get_image_thumbnail(db: DbDep, storage: StorageDep, image_id: uuid.UUID) -> Response:
    image = image_service.get_image(db, image_id)
    return _serve(storage, image.thumbnail_key, "image/webp", f"{image.sha256}-thumb")


@router.get(
    "/images/{image_id}/cutout",
    summary="The picture local AI generation receives (after a cut-out check)",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}, "description": "Cut-out preview"}},
)
def get_image_cutout(db: DbDep, storage: StorageDep, image_id: uuid.UUID) -> Response:
    image = image_service.get_image(db, image_id)
    found = image.analysis.get("cutout")
    if not isinstance(found, dict):
        raise NotFoundError("This photo's cut-out has not been checked yet.")
    response = _serve(
        storage,
        image_cutout_key(image.project_id, image.id),
        "image/png",
        f"{image.sha256}-cutout-{found.get('checked_at', '')}",
    )
    response.headers["Cache-Control"] = "private, no-cache"  # replaced when checked again
    return response


@router.delete(
    "/images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a photo and its derived files",
)
def delete_image(db: DbDep, storage: StorageDep, image_id: uuid.UUID) -> Response:
    image_service.delete_image(db, storage, image_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
