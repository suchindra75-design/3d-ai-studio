import re
import shutil
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ai3d.api.deps import DbDep, SettingsDep, StorageDep
from ai3d.models.enums import ProjectStatus
from ai3d.schemas.capture import CaptureAssessment
from ai3d.schemas.project import ProjectCreate, ProjectList, ProjectRead, ProjectUpdate
from ai3d.services import capture as capture_service
from ai3d.services import projects as project_service
from ai3d.services import transfer as transfer_service

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=ProjectList, summary="List projects")
def list_projects(
    db: DbDep,
    status_filter: Annotated[ProjectStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=200, description="Name contains")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProjectList:
    items, total = project_service.list_projects(
        db, status=status_filter, query=q, limit=limit, offset=offset
    )
    return ProjectList(items=items, total=total, limit=limit, offset=offset)


@router.post(
    "", response_model=ProjectRead, status_code=status.HTTP_201_CREATED, summary="Create a project"
)
def create_project(db: DbDep, body: ProjectCreate) -> ProjectRead:
    return project_service.create_project(db, body)


@router.post(
    "/restore",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Restore a project from a backup ZIP",
    description="Creates a new project (new IDs). Photos go through the normal upload "
    "checks and every model file is re-opened and checked; if anything fails, nothing "
    "is kept.",
)
def restore_project(
    db: DbDep,
    storage: StorageDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="Backup ZIP made by this app")],
) -> ProjectRead:
    return transfer_service.restore_project(db, storage, settings, file.file)


@router.get(
    "/{project_id}/backup",
    summary="Download the project as a backup ZIP",
    description="manifest.json (settings, models, versions, notes, quality reports), the "
    "original photos and every version's GLB. Exports and job history are not included.",
    response_class=FileResponse,
)
def backup_project(
    db: DbDep, storage: StorageDep, settings: SettingsDep, project_id: uuid.UUID
) -> FileResponse:
    project = project_service.get_project(db, project_id)
    archive = transfer_service.backup_project(db, storage, settings, project_id)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", project.name).strip("-").lower()[:80] or "project"
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"{slug}-backup.zip",
        background=BackgroundTask(shutil.rmtree, archive.parent, ignore_errors=True),
    )


@router.get("/{project_id}", response_model=ProjectRead, summary="Get a project")
def get_project(db: DbDep, project_id: uuid.UUID) -> ProjectRead:
    return project_service.read_project(db, project_id)


@router.patch("/{project_id}", response_model=ProjectRead, summary="Update a project")
def update_project(db: DbDep, project_id: uuid.UUID, body: ProjectUpdate) -> ProjectRead:
    return project_service.update_project(db, project_id, body)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project and all of its files",
)
def delete_project(db: DbDep, storage: StorageDep, project_id: uuid.UUID) -> Response:
    project_service.delete_project(db, storage, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{project_id}/capture-assessment",
    response_model=CaptureAssessment,
    summary="Assess whether the project's photos are ready for reconstruction",
)
def capture_assessment(db: DbDep, storage: StorageDep, project_id: uuid.UUID) -> CaptureAssessment:
    return capture_service.assess_capture(db, project_id, storage)


@router.post(
    "/{project_id}/capture-assessment/overlap",
    response_model=CaptureAssessment,
    summary="Measure overlap between the photos (feature matching)",
    description="Matches features between photos on this machine (OpenCV SIFT + RANSAC) and "
    "caches the result for the current photo set. Takes a few seconds per 10 photos.",
)
def measure_overlap(
    db: DbDep, storage: StorageDep, settings: SettingsDep, project_id: uuid.UUID
) -> CaptureAssessment:
    return capture_service.measure_overlap(db, storage, settings, project_id)


@router.post(
    "/{project_id}/capture-assessment/cutout",
    response_model=CaptureAssessment,
    summary="Preview the cut-out local generation would make (single-image projects)",
    description="Runs local generation's background removal on this machine for each photo "
    "without a current result, checks framing and how well the object stands out, and "
    "stores the picture the AI would receive (`/images/{id}/cutout`). Nothing is uploaded. "
    "Takes a few seconds per photo.",
)
def check_cutout(
    db: DbDep, storage: StorageDep, settings: SettingsDep, project_id: uuid.UUID
) -> CaptureAssessment:
    return capture_service.check_cutouts(db, storage, settings, project_id)
