"""wave1 tool usage notes, sample invocations, connection rotation stamps

Revision ID: a9c4d2e1f7b5
Revises: d5ffa516aec4
Create Date: 2026-10-05 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9c4d2e1f7b5"
down_revision: str | None = "d5ffa516aec4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tools", sa.Column("usage_note", sa.Text(), nullable=True))
    op.add_column("tools", sa.Column("example_invocation", postgresql.JSONB(), nullable=True))
    op.add_column(
        "connections",
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("CREATE INDEX ix_audit_logs_org_id_token_id_ts ON audit_logs (org_id, token_id, ts)")
    op.execute(
        "CREATE INDEX ix_audit_logs_org_id_agent_name_ts ON audit_logs (org_id, agent_name, ts)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_logs_org_id_agent_name_ts")
    op.execute("DROP INDEX IF EXISTS ix_audit_logs_org_id_token_id_ts")
    op.drop_column("connections", "last_rotated_at")
    op.drop_column("tools", "example_invocation")
    op.drop_column("tools", "usage_note")
