"""Monthly range-partition helpers for `audit_logs` (and friends later).

A partition covers one calendar month. The names are computed from the
`ts` partition key and are stable in both jobs and Alembic migrations:

    audit_logs_y2026m01
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

PARENT_TABLE = "audit_logs"
_SUFFIX = re.compile(r"^y(\d{4})m(\d{2})$")


class PartitionError(Exception):
    pass


def partition_name(year: int, month: int, *, parent: str = PARENT_TABLE) -> str:
    if not 1 <= month <= 12:
        raise PartitionError(f"month {month} is not in 1..12")
    return f"{parent}_y{year:04d}m{month:02d}"


def is_partition_name(name: str, *, parent: str = PARENT_TABLE) -> bool:
    return name.startswith(f"{parent}_") and _SUFFIX.match(name[len(parent) + 1 :]) is not None


def parse_partition_name(name: str, *, parent: str = PARENT_TABLE) -> tuple[int, int] | None:
    if not is_partition_name(name, parent=parent):
        return None
    match = _SUFFIX.match(name[len(parent) + 1 :])
    assert match is not None
    return int(match.group(1)), int(match.group(2))


def month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=dt.tzinfo or UTC)


def month_end(dt: datetime) -> datetime:
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return datetime(year, month, 1, tzinfo=dt.tzinfo or UTC)


def partition_month_range(dt: datetime) -> tuple[datetime, datetime]:
    return month_start(dt), month_end(dt)


def partition_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    return start, month_end(start)
