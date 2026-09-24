"""oauth_states

Revision ID: e8a1c4f27b93
Revises: 4f2a9c81b6d7
Create Date: 2026-09-24 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8a1c4f27b93"
down_revision: str | None = "4f2a9c81b6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("pkce_verifier", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            ["user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_oauth_states_state", "oauth_states", ["state"], unique=True)
    op.create_index("ix_oauth_states_org_id", "oauth_states", ["org_id"], unique=False)
    op.create_index("ix_oauth_states_user_id", "oauth_states", ["user_id"], unique=False)
    op.create_index("ix_oauth_states_service_id", "oauth_states", ["service_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_oauth_states_service_id", table_name="oauth_states")
    op.drop_index("ix_oauth_states_user_id", table_name="oauth_states")
    op.drop_index("ix_oauth_states_org_id", table_name="oauth_states")
    op.drop_index("uq_oauth_states_state", table_name="oauth_states")
    op.drop_table("oauth_states")
