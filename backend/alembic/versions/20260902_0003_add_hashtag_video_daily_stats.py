"""add hashtag video daily stats

Revision ID: 202609020003
Revises: 202608010002
Create Date: 2026-09-02 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "202609020003"
down_revision: Union[str, None] = "202608010002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hashtag_video_daily_stats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("hashtag", sa.String(length=100), nullable=False),
        sa.Column("video_id", sa.String(length=100), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("channel_title", sa.String(length=255), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("thumbnail_url", sa.String(length=1024), nullable=True),
        sa.Column("views", sa.BigInteger(), nullable=False),
        sa.Column("view_delta", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "video_id",
            "snapshot_date",
            name="uq_hashtag_video_daily_stats_project_video_date",
        ),
    )
    op.create_index(
        "ix_hashtag_video_daily_stats_project_date",
        "hashtag_video_daily_stats",
        ["project_id", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hashtag_video_daily_stats_project_date",
        table_name="hashtag_video_daily_stats",
    )
    op.drop_table("hashtag_video_daily_stats")
