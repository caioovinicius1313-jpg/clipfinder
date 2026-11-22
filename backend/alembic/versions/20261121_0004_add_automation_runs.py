"""add automation runs

Revision ID: 202611210004
Revises: 202609020003
Create Date: 2026-11-21 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "202611210004"
down_revision: Union[str, None] = "202609020003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "automation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_video_id", sa.Uuid(), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=False),
        sa.Column("hashtag", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("total_references", sa.Integer(), nullable=False),
        sa.Column("ready_references", sa.Integer(), nullable=False),
        sa.Column("jobs_started", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_video_id"], ["source_videos.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_automation_runs_project_id", "automation_runs", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_automation_runs_project_id", table_name="automation_runs")
    op.drop_table("automation_runs")
