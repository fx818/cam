"""service_visibility

Revision ID: b7c41e9a2d05
Revises: dd8d89a374eb
Create Date: 2026-09-24 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c41e9a2d05"
down_revision: str | None = "dd8d89a374eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_visibility",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_id", "user_id", name="uq_service_visibility_service_user"),
    )
    op.create_index("ix_service_visibility_org_id", "service_visibility", ["org_id"], unique=False)
    op.create_index(
        "ix_service_visibility_service_id", "service_visibility", ["service_id"], unique=False
    )
    op.create_index(
        "ix_service_visibility_user_id", "service_visibility", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_service_visibility_user_id", table_name="service_visibility")
    op.drop_index("ix_service_visibility_service_id", table_name="service_visibility")
    op.drop_index("ix_service_visibility_org_id", table_name="service_visibility")
    op.drop_table("service_visibility")
