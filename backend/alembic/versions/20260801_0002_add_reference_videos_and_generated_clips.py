"""add reference videos and generated clips

Revision ID: 202608010002
Revises: 202607310001
Create Date: 2026-08-01 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "202608010002"
down_revision: Union[str, None] = "202607310001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("source_videos", sa.Column("duration", sa.Float(), nullable=True))
    op.create_table(
        "reference_videos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("external_id", sa.String(length=100), nullable=True),
        sa.Column("external_url", sa.String(length=1024), nullable=True),
        sa.Column("thumbnail_url", sa.String(length=1024), nullable=True),
        sa.Column("channel_title", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="uploaded", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reference_videos_project_id", "reference_videos", ["project_id"])
    op.create_table(
        "generated_clips",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_video_id", sa.Uuid(), nullable=False),
        sa.Column("reference_video_id", sa.Uuid(), nullable=False),
        sa.Column("original_start_seconds", sa.Float(), nullable=True),
        sa.Column("original_end_seconds", sa.Float(), nullable=True),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="processing", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_video_id"], ["source_videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reference_video_id"], ["reference_videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generated_clips_project_id", "generated_clips", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_generated_clips_project_id", table_name="generated_clips")
    op.drop_table("generated_clips")
    op.drop_index("ix_reference_videos_project_id", table_name="reference_videos")
    op.drop_table("reference_videos")
    op.drop_column("source_videos", "duration")
