import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ai3d.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from ai3d.models.project import Project


class Scene(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A CGI shot made from a prompt: its plan, its objects (models of the project)
    and the rendered files (video, still, .blend, GLB), which live in storage under
    ``projects/<id>/scenes/<scene id>/``."""

    __tablename__ = "scenes"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    # The job that made (or is making) it.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True
    )
    # Rendered again from another scene's objects with different shot settings.
    source_scene_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scenes.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    # The scene plan (processing/scene_plan.py), with each object's model once made.
    plan: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # What the user chose: length, quality, overrides of the plan.
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Blender's report of the render (frames, engines, timings); None until rendered.
    render: Mapped[dict[str, Any] | None]
    # name -> {"key", "mime_type", "size_bytes", "sha256"}
    files: Mapped[dict[str, Any]] = mapped_column(default=dict)

    project: Mapped["Project"] = relationship(back_populates="scenes")
