"""track YouTube reference download format strategy

Revision ID: 202611250006
Revises: 202611210005
Create Date: 2026-11-25 00:06:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "202611250006"
down_revision: Union[str, None] = "202611210005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reference_videos",
        sa.Column("download_format_version", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reference_videos", "download_format_version")
