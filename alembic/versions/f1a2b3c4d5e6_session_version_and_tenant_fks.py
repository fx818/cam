"""users.session_version and composite tenant foreign keys

Revision ID: f1a2b3c4d5e6
Revises: a9c4d2e1f7b5
Create Date: 2026-10-12 09:00:00.000000

Adds a revocable-session counter to `users` (R17 schema support) and composite
tenant foreign keys that keep a child row's `org_id` equal to its parent's
(R25 schema support).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "a9c4d2e1f7b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    # Composite tenant key: referenced by the child-table foreign keys below so
    # that `org_id` on the child must equal `org_id` on the service.
    op.create_unique_constraint("uq_services_id_org_id", "services", ["id", "org_id"])
    op.create_foreign_key(
        "fk_tools_service_org",
        "tools",
        "services",
        ["service_id", "org_id"],
        ["id", "org_id"],
    )
    op.create_foreign_key(
        "fk_connections_service_org",
        "connections",
        "services",
        ["service_id", "org_id"],
        ["id", "org_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_connections_service_org", "connections", type_="foreignkey")
    op.drop_constraint("fk_tools_service_org", "tools", type_="foreignkey")
    op.drop_constraint("uq_services_id_org_id", "services", type_="unique")
    op.drop_column("users", "session_version")
