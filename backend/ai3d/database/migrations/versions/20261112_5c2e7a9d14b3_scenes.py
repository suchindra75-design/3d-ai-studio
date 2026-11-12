"""scenes: CGI shots made from a prompt

The new input mode ("prompt") and job kind ("scene") need no change: enums are
stored as plain strings without a database constraint.

Revision ID: 5c2e7a9d14b3
Revises: a186cb9fc3f6
Create Date: 2026-11-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

import ai3d.database.base

revision: str = "5c2e7a9d14b3"
down_revision: str | None = "a186cb9fc3f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "scenes",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("source_scene_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("plan", _JSON, nullable=False),
        sa.Column("settings", _JSON, nullable=False),
        sa.Column("render", _JSON, nullable=True),
        sa.Column("files", _JSON, nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", ai3d.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column("updated_at", ai3d.database.base.UTCDateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["generation_jobs.id"],
            name=op.f("fk_scenes_job_id_generation_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_scenes_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_scene_id"],
            ["scenes.id"],
            name=op.f("fk_scenes_source_scene_id_scenes"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scenes")),
    )
    with op.batch_alter_table("scenes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_scenes_job_id"), ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_scenes_project_id"), ["project_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("scenes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scenes_project_id"))
        batch_op.drop_index(batch_op.f("ix_scenes_job_id"))
    op.drop_table("scenes")
