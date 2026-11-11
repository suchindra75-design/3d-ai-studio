"""The HTTP API the app talks to (version 1). Every request needs the token.

GET    /v1/health                  backend, model, GPU, queue
POST   /v1/jobs                    photos (multipart ``photos``), ``roles``, ``settings``
GET    /v1/jobs/{id}               status, stage, progress, message, files
POST   /v1/jobs/{id}/cancel
GET    /v1/jobs/{id}/files/{name}  model.glb, cutout-<role>.png, result.json
DELETE /v1/jobs/{id}               delete a finished job's files
"""

import hmac
import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from ai3d_gpu import API_VERSION
from ai3d_gpu.backends.base import Backend, GenerationSettings
from ai3d_gpu.config import ServerConfig
from ai3d_gpu.jobs import TERMINAL, Job, JobQueue, QueueFullError

ROLES = ("source", "front", "left", "back", "right")
CHUNK = 1024 * 1024


def _authorised(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    expected = f"Bearer {request.app.state.config.token}"
    if authorization is None or not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Missing or wrong token.")


router = APIRouter(prefix="/v1", dependencies=[Depends(_authorised)])


def _queue(request: Request) -> JobQueue:
    queue: JobQueue = request.app.state.queue
    return queue


def _job(request: Request, job_id: str) -> Job:
    job = _queue(request).get(job_id)
    if job is None:
        raise HTTPException(404, "No such job (finished jobs are kept for a day).")
    return job


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    backend: Backend = request.app.state.backend
    return {
        "ok": True,
        "api_version": API_VERSION,
        "backend": backend.name,
        "model": backend.model,
        "device": backend.device,
        **_queue(request).counts(),
    }


def _roles(given: str, count: int, max_images: int) -> list[str]:
    names = [r.strip() for r in given.split(",") if r.strip()] or ["source"] * count
    if not 1 <= count <= max_images:
        raise HTTPException(422, f"Send 1–{max_images} photos.")
    if len(names) != count or any(r not in ROLES for r in names):
        raise HTTPException(422, f"Give one role per photo: {', '.join(ROLES)}.")
    if count > 1 and (names[0] != "front" or len(set(names)) != count):
        raise HTTPException(
            422, "With several photos the first is 'front' and each role is used once."
        )
    return names


async def _read_photo(photo: UploadFile, max_bytes: int) -> bytes:
    data = bytearray()
    while chunk := await photo.read(CHUNK):
        data.extend(chunk)
        if len(data) > max_bytes:
            raise HTTPException(413, f"{photo.filename}: larger than the server accepts.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, f"{photo.filename}: not a usable image.") from exc
    return bytes(data)


@router.post("/jobs", status_code=202)
async def submit(
    request: Request,
    photos: Annotated[list[UploadFile], File()],
    roles: Annotated[str, Form()] = "",
    settings: Annotated[str, Form()] = "{}",
) -> dict[str, Any]:
    config: ServerConfig = request.app.state.config
    names = _roles(roles, len(photos), config.max_images)
    try:
        parsed = GenerationSettings.model_validate(json.loads(settings or "{}"))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(422, f"Invalid settings: {exc}") from exc
    uploaded = [
        (role, await _read_photo(photo, config.max_upload_bytes))
        for role, photo in zip(names, photos, strict=True)
    ]
    try:
        job = _queue(request).submit(uploaded, parsed)
    except QueueFullError as exc:
        raise HTTPException(503, "The GPU queue is full; try again in a few minutes.") from exc
    return job.read()


@router.get("/jobs/{job_id}")
def status(request: Request, job_id: str) -> dict[str, Any]:
    return _job(request, job_id).read()


@router.post("/jobs/{job_id}/cancel")
def cancel(request: Request, job_id: str) -> dict[str, Any]:
    job = _queue(request).cancel(job_id)
    if job is None:
        raise HTTPException(404, "No such job.")
    return job.read()


@router.get("/jobs/{job_id}/files/{name}")
def file(request: Request, job_id: str, name: str) -> FileResponse:
    job = _job(request, job_id)
    if job.result is None or name not in [*job.result["files"], "result.json"]:
        raise HTTPException(404, "No such file for this job.")
    return FileResponse(job.folder / name)


@router.delete("/jobs/{job_id}", status_code=204)
def delete(request: Request, job_id: str) -> Response:
    job = _job(request, job_id)
    if job.status not in TERMINAL:
        raise HTTPException(409, "Cancel the job first.")
    _queue(request).delete(job_id)
    return Response(status_code=204)


def create_app(config: ServerConfig, backend: Backend) -> FastAPI:
    queue = JobQueue(
        backend, config.work_dir, max_queue=config.max_queue, keep_seconds=config.keep_seconds
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        queue.stop()

    app = FastAPI(
        title="AI 3D Studio GPU server",
        version=API_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.backend = backend
    app.state.queue = queue
    app.include_router(router)
    return app
