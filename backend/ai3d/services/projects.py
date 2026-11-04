"""Project use cases."""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai3d.core.errors import NotFoundError
from ai3d.core.timeutil import utcnow
from ai3d.models import Image, Project
from ai3d.models.enums import ImageKind, ProjectStatus
from ai3d.schemas.project import ProjectCreate, ProjectRead, ProjectUpdate
from ai3d.storage import StorageBackend, project_prefix

log = logging.getLogger(__name__)


_image_count = (
    select(func.count(Image.id))
    .where(Image.project_id == Project.id, Image.kind == ImageKind.ORIGINAL)
    .correlate(Project)
    .scalar_subquery()
)
_image_bytes = (
    select(func.coalesce(func.sum(Image.size_bytes), 0))
    .where(Image.project_id == Project.id, Image.kind == ImageKind.ORIGINAL)
    .correlate(Project)
    .scalar_subquery()
)
_cover_image = (
    select(Image.id)
    .where(Image.project_id == Project.id, Image.kind == ImageKind.ORIGINAL)
    .order_by(Image.position, Image.created_at)
    .limit(1)
    .correlate(Project)
    .scalar_subquery()
)


def _to_read(project: Project, count: int, size: int, cover: uuid.UUID | None) -> ProjectRead:
    read = ProjectRead.model_validate(project)
    read.image_count = count
    read.total_image_bytes = int(size or 0)
    read.cover_image_id = cover
    return read


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_projects(
    db: Session,
    *,
    status: ProjectStatus | None,
    query: str | None,
    limit: int,
    offset: int,
) -> tuple[list[ProjectRead], int]:
    conditions = []
    if status is not None:
        conditions.append(Project.status == status)
    if query:
        conditions.append(Project.name.ilike(f"%{_escape_like(query)}%", escape="\\"))

    total = db.scalar(select(func.count(Project.id)).where(*conditions)) or 0
    rows = db.execute(
        select(Project, _image_count, _image_bytes, _cover_image)
        .where(*conditions)
        .order_by(Project.updated_at.desc(), Project.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [_to_read(p, c, s, cover) for p, c, s, cover in rows], total


def get_project(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project not found.", details={"project_id": str(project_id)})
    return project


def read_project(db: Session, project_id: uuid.UUID) -> ProjectRead:
    row = db.execute(
        select(Project, _image_count, _image_bytes, _cover_image).where(Project.id == project_id)
    ).first()
    if row is None:
        raise NotFoundError("Project not found.", details={"project_id": str(project_id)})
    project, count, size, cover = row
    return _to_read(project, count, size, cover)


def create_project(db: Session, data: ProjectCreate) -> ProjectRead:
    project = Project(**data.model_dump())
    db.add(project)
    db.commit()
    log.info("project created", extra={"project_id": str(project.id)})
    return _to_read(project, 0, 0, None)


def update_project(db: Session, project_id: uuid.UUID, data: ProjectUpdate) -> ProjectRead:
    project = get_project(db, project_id)
    changes = data.model_dump(exclude_unset=True)
    if "name" in changes and not changes["name"]:
        changes.pop("name")
    if "status" in changes and changes["status"] != project.status:
        project.archived_at = utcnow() if changes["status"] == ProjectStatus.ARCHIVED else None
    for field, value in changes.items():
        if value is not None:
            setattr(project, field, value)
    db.commit()
    return read_project(db, project_id)


def touch_project(db: Session, project_id: uuid.UUID) -> None:
    project = db.get(Project, project_id)
    if project is not None:
        project.updated_at = utcnow()


def delete_project(db: Session, storage: StorageBackend, project_id: uuid.UUID) -> None:
    """Delete a project, its rows (FK cascades) and then its files.

    Rows go first: if file removal fails we are left with orphaned files,
    never with rows pointing at missing files.
    """
    project = get_project(db, project_id)
    db.delete(project)
    db.commit()
    try:
        storage.delete_prefix(project_prefix(project_id))
    except OSError:
        log.exception("failed to delete project files", extra={"project_id": str(project_id)})
    log.info("project deleted", extra={"project_id": str(project_id)})
