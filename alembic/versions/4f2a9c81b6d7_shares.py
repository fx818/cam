"""shares

Revision ID: 4f2a9c81b6d7
Revises: b7c41e9a2d05
Create Date: 2026-09-24 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f2a9c81b6d7"
down_revision: str | None = "b7c41e9a2d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shares",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("grantee_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
        ),
        sa.ForeignKeyConstraint(
            ["grantee_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_shares_active_connection_grantee",
        "shares",
        ["connection_id", "grantee_user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_shares_org_id", "shares", ["org_id"], unique=False)
    op.create_index("ix_shares_connection_id", "shares", ["connection_id"], unique=False)
    op.create_index("ix_shares_grantee_user_id", "shares", ["grantee_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_shares_grantee_user_id", table_name="shares")
    op.drop_index("ix_shares_connection_id", table_name="shares")
    op.drop_index("ix_shares_org_id", table_name="shares")
    op.drop_index("uq_shares_active_connection_grantee", table_name="shares")
    op.drop_table("shares")
