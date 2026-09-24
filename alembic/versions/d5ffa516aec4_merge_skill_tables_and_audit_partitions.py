"""merge skill tables and audit partitions

Revision ID: d5ffa516aec4
Revises: b8d2f1a6c4e7, c3e5f8a91b24
Create Date: 2026-09-24 21:27:17.853538

"""

from collections.abc import Sequence

revision: str = "d5ffa516aec4"
down_revision: str | Sequence[str] | None = ("b8d2f1a6c4e7", "c3e5f8a91b24")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
