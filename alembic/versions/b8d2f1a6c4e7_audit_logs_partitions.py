"""audit_logs monthly RANGE partitions on ts

Revision ID: b8d2f1a6c4e7
Revises: e8a1c4f27b93
Create Date: 2026-09-24 21:30:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "b8d2f1a6c4e7"
down_revision: str | None = "e8a1c4f27b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "uuid"),
    ("org_id", "uuid"),
    ("ts", "timestamptz"),
    ("user_id", "uuid"),
    ("token_id", "uuid"),
    ("agent_name", "varchar"),
    ("agent_version", "varchar"),
    ("action", "varchar"),
    ("service_id", "uuid"),
    ("connection_id", "uuid"),
    ("owner_user_id", "uuid"),
    ("tool_name", "varchar"),
    ("skill_version_id", "uuid"),
    ("args", "jsonb"),
    ("status", "varchar"),
    ("error_code", "varchar"),
    ("error_message", "text"),
    ("latency_ms", "integer"),
    ("response_bytes", "integer"),
    ("truncated", "boolean"),
)
_COLUMNS_SQL = ",\n".join(
    f"{name} {kind}"
    + (" DEFAULT now()" if name == "ts" else "")
    + (" NOT NULL" if name in ("id", "org_id", "ts", "action") else "")
    for name, kind in _COLUMNS
)
_FK_REFERENCES: dict[str, str] = {
    "org_id": "orgs",
    "user_id": "users",
    "token_id": "cam_tokens",
    "service_id": "services",
    "connection_id": "connections",
    "owner_user_id": "users",
}
_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_audit_logs_ts", "(ts)"),
    ("ix_audit_logs_org_id_user_id_ts", "(org_id, user_id, ts)"),
    ("ix_audit_logs_org_id_owner_user_id_ts", "(org_id, owner_user_id, ts)"),
    ("ix_audit_logs_org_id_service_id_ts", "(org_id, service_id, ts)"),
)


def _add_fks(table: str) -> None:
    for column, referenced in _FK_REFERENCES.items():
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_fkey "
                f"FOREIGN KEY ({column}) REFERENCES {referenced}(id)"
            )
        )


def _create_indexes(table: str) -> None:
    for name, columns in _INDEXES:
        op.execute(sa.text(f"CREATE INDEX {name} ON {table} {columns}"))


def _upcoming_months(count: int) -> list[tuple[int, int]]:
    now = datetime.now(tz=UTC)
    months: list[tuple[int, int]] = []
    for offset in range(count):
        month = now.month + offset
        year = now.year
        while month > 12:
            month -= 12
            year += 1
        months.append((year, month))
    return months


def _create_month_partition(year: int, month: int) -> None:
    from cam.db.partitions import partition_bounds, partition_name

    start, end = partition_bounds(year, month)
    name = partition_name(year, month)
    op.execute(
        sa.text(
            f"CREATE TABLE {name} PARTITION OF audit_logs "
            f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
        )
    )


def _rename_fks(old_prefix: str) -> None:
    for column in _FK_REFERENCES:
        op.execute(
            sa.text(
                "ALTER TABLE audit_logs RENAME CONSTRAINT "
                f"{old_prefix}_{column}_fkey TO audit_logs_{column}_fkey"
            )
        )


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE TABLE audit_logs_new (\n"
            f"{_COLUMNS_SQL},\n"
            "    PRIMARY KEY (id, ts)\n"
            ") PARTITION BY RANGE (ts)"
        )
    )
    _add_fks("audit_logs_new")
    op.execute(sa.text("INSERT INTO audit_logs_new SELECT * FROM audit_logs"))
    op.execute(sa.text("DROP TABLE audit_logs"))
    op.execute(sa.text("ALTER TABLE audit_logs_new RENAME TO audit_logs"))
    op.execute(sa.text("ALTER INDEX audit_logs_new_pkey RENAME TO audit_logs_pkey"))
    _rename_fks("audit_logs_new")
    for year, month in _upcoming_months(2):
        _create_month_partition(year, month)
    _create_indexes("audit_logs")


def downgrade() -> None:
    op.execute(
        sa.text(f"CREATE TABLE audit_logs_plain (\n{_COLUMNS_SQL},\n    PRIMARY KEY (id)\n)")
    )
    _add_fks("audit_logs_plain")
    op.execute(sa.text("INSERT INTO audit_logs_plain SELECT * FROM audit_logs"))
    op.execute(sa.text("DROP TABLE audit_logs"))
    op.execute(sa.text("ALTER TABLE audit_logs_plain RENAME TO audit_logs"))
    _rename_fks("audit_logs_plain")
    _create_indexes("audit_logs")
