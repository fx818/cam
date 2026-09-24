"""skills tables

Revision ID: c3e5f8a91b24
Revises: e8a1c4f27b93
Create Date: 2026-09-24 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e5f8a91b24"
down_revision: str | None = "e8a1c4f27b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(tier = 'personal' AND owner_user_id IS NOT NULL)"
            " OR (tier = 'global' AND owner_user_id IS NULL)",
            name="owner_by_tier",
        ),
        sa.CheckConstraint("tier IN ('global', 'personal')", name="tier_valid"),
        sa.CheckConstraint("visibility IN ('all', 'restricted')", name="visibility_valid"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_skills_org_owner_slug_personal",
        "skills",
        ["org_id", "owner_user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("tier = 'personal'"),
    )
    op.create_index(
        "uq_skills_org_slug_global",
        "skills",
        ["org_id", "slug"],
        unique=True,
        postgresql_where=sa.text("tier = 'global'"),
    )
    op.create_index("ix_skills_org_id", "skills", ["org_id"], unique=False)
    op.create_index("ix_skills_owner_user_id", "skills", ["owner_user_id"], unique=False)
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_skill_versions_skill_version", "skill_versions", ["skill_id", "version"], unique=True
    )
    op.create_index("ix_skill_versions_org_id", "skill_versions", ["org_id"], unique=False)
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"], unique=False)
    op.create_table(
        "skill_files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["skill_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_skill_files_version_path", "skill_files", ["version_id", "path"], unique=True
    )
    op.create_index("ix_skill_files_org_id", "skill_files", ["org_id"], unique=False)
    op.create_index("ix_skill_files_version_id", "skill_files", ["version_id"], unique=False)
    op.create_table(
        "skill_requires",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_skill_requires_skill_service", "skill_requires", ["skill_id", "service_id"], unique=True
    )
    op.create_index("ix_skill_requires_org_id", "skill_requires", ["org_id"], unique=False)
    op.create_index("ix_skill_requires_skill_id", "skill_requires", ["skill_id"], unique=False)
    op.create_index("ix_skill_requires_service_id", "skill_requires", ["service_id"], unique=False)
    op.create_table(
        "skill_visibility",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
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
            ["skill_id"],
            ["skills.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "user_id", name="uq_skill_visibility_skill_user"),
    )
    op.create_index("ix_skill_visibility_org_id", "skill_visibility", ["org_id"], unique=False)
    op.create_index("ix_skill_visibility_skill_id", "skill_visibility", ["skill_id"], unique=False)
    op.create_index("ix_skill_visibility_user_id", "skill_visibility", ["user_id"], unique=False)
    op.create_table(
        "skill_shares",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("grantee_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["grantee_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_skill_shares_active_skill_grantee",
        "skill_shares",
        ["skill_id", "grantee_user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index("ix_skill_shares_org_id", "skill_shares", ["org_id"], unique=False)
    op.create_index("ix_skill_shares_skill_id", "skill_shares", ["skill_id"], unique=False)
    op.create_index(
        "ix_skill_shares_grantee_user_id", "skill_shares", ["grantee_user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_skill_shares_grantee_user_id", table_name="skill_shares")
    op.drop_index("ix_skill_shares_skill_id", table_name="skill_shares")
    op.drop_index("ix_skill_shares_org_id", table_name="skill_shares")
    op.drop_index(
        "uq_skill_shares_active_skill_grantee",
        table_name="skill_shares",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("skill_shares")
    op.drop_index("ix_skill_visibility_user_id", table_name="skill_visibility")
    op.drop_index("ix_skill_visibility_skill_id", table_name="skill_visibility")
    op.drop_index("ix_skill_visibility_org_id", table_name="skill_visibility")
    op.drop_table("skill_visibility")
    op.drop_index("ix_skill_requires_service_id", table_name="skill_requires")
    op.drop_index("ix_skill_requires_skill_id", table_name="skill_requires")
    op.drop_index("ix_skill_requires_org_id", table_name="skill_requires")
    op.drop_index("uq_skill_requires_skill_service", table_name="skill_requires")
    op.drop_table("skill_requires")
    op.drop_index("ix_skill_files_version_id", table_name="skill_files")
    op.drop_index("ix_skill_files_org_id", table_name="skill_files")
    op.drop_index("uq_skill_files_version_path", table_name="skill_files")
    op.drop_table("skill_files")
    op.drop_index("ix_skill_versions_skill_id", table_name="skill_versions")
    op.drop_index("ix_skill_versions_org_id", table_name="skill_versions")
    op.drop_index("uq_skill_versions_skill_version", table_name="skill_versions")
    op.drop_table("skill_versions")
    op.drop_index("ix_skills_owner_user_id", table_name="skills")
    op.drop_index("ix_skills_org_id", table_name="skills")
    op.drop_index(
        "uq_skills_org_slug_global",
        table_name="skills",
        postgresql_where=sa.text("tier = 'global'"),
    )
    op.drop_index(
        "uq_skills_org_owner_slug_personal",
        table_name="skills",
        postgresql_where=sa.text("tier = 'personal'"),
    )
    op.drop_table("skills")
