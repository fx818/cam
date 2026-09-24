"""settings table

Revision ID: 1ce689a33f60
Revises:
Create Date: 2026-09-24 11:11:44.894198

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1ce689a33f60"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("settings")
