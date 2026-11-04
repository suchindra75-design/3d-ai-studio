import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from ai3d.api.deps import DbDep, SettingsDep, StorageDep
from ai3d.models.enums import ProjectStatus
from ai3d.schemas.capture import CaptureAssessment
from ai3d.schemas.project import ProjectCreate, ProjectList, ProjectRead, ProjectUpdate
from ai3d.services import capture as capture_service
from ai3d.services import projects as project_service

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
