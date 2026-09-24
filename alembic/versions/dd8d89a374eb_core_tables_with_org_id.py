"""core tables with org_id

Revision ID: dd8d89a374eb
Revises: 1ce689a33f60
Create Date: 2026-09-24 11:40:09.961250

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "dd8d89a374eb"
down_revision: str | None = "1ce689a33f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_ORG_ID = uuid.uuid5(uuid.NAMESPACE_URL, "https://cam.local/org/indiamart")


def upgrade() -> None:
    op.create_table(
        "orgs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("data_key_enc", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("handle", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('person', 'team')", name="kind_valid"),
        sa.CheckConstraint("role IN ('admin', 'user')", name="role_valid"),
        sa.CheckConstraint("status IN ('active', 'deactivated')", name="status_valid"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("org_id", "handle", name="uq_users_org_id_handle"),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"], unique=False)
    op.create_table(
        "cam_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=8), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_cam_tokens_org_id", "cam_tokens", ["org_id"], unique=False)
    op.create_index("ix_cam_tokens_user_id", "cam_tokens", ["user_id"], unique=False)
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_login_attempts_org_id", "login_attempts", ["org_id"], unique=False)
    op.create_table(
        "services",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("transport", sa.String(), nullable=True),
        sa.Column("spec_url", sa.String(), nullable=True),
        sa.Column("auth_type", sa.String(), nullable=False),
        sa.Column(
            "auth_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("visibility", sa.String(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("timeout_ms", sa.Integer(), server_default=sa.text("60000"), nullable=False),
        sa.Column(
            "response_cap_tokens", sa.Integer(), server_default=sa.text("50000"), nullable=False
        ),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("health", sa.String(), server_default=sa.text("'unknown'"), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(tier = 'personal' AND owner_user_id IS NOT NULL)"
            " OR (tier != 'personal' AND owner_user_id IS NULL)",
            name="owner_by_tier",
        ),
        sa.CheckConstraint(
            "auth_type IN ('none', 'static-header', 'bearer', 'api-key', 'basic', 'oauth')",
            name="auth_type_valid",
        ),
        sa.CheckConstraint(
            "health IN ('unknown', 'healthy', 'failing', 'needs_reconnect')", name="health_valid"
        ),
        sa.CheckConstraint("kind IN ('mcp', 'rest')", name="kind_valid"),
        sa.CheckConstraint("status IN ('active', 'disabled', 'deleted')", name="status_valid"),
        sa.CheckConstraint("tier IN ('global', 'template', 'personal')", name="tier_valid"),
        sa.CheckConstraint(
            "transport IS NULL OR transport IN ('streamable-http', 'sse')", name="transport_valid"
        ),
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
    op.create_index("ix_services_org_id", "services", ["org_id"], unique=False)
    op.create_index("ix_services_owner_user_id", "services", ["owner_user_id"], unique=False)
    op.create_index(
        "uq_services_org_owner_slug_personal",
        "services",
        ["org_id", "owner_user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("tier = 'personal'"),
    )
    op.create_index(
        "uq_services_org_slug_global_template",
        "services",
        ["org_id", "slug"],
        unique=True,
        postgresql_where=sa.text("tier IN ('global', 'template')"),
    )
    op.create_table(
        "url_allowlist",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_url_allowlist_org_id", "url_allowlist", ["org_id"], unique=False)
    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("refresh_enc", sa.LargeBinary(), nullable=True),
        sa.Column("key_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'connected'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('connected', 'needs_reconnect', 'disabled')", name="status_valid"
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
    )
    op.create_index("ix_connections_org_id", "connections", ["org_id"], unique=False)
    op.create_index("ix_connections_service_id", "connections", ["service_id"], unique=False)
    op.create_index("ix_connections_user_id", "connections", ["user_id"], unique=False)
    op.create_index(
        "uq_connections_service_org_credential",
        "connections",
        ["service_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "uq_connections_service_user",
        "connections",
        ["service_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_table(
        "tools",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("upstream_name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("description_override", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rest_binding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'pending_review', 'removed_upstream')", name="status_valid"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tools_org_id", "tools", ["org_id"], unique=False)
    op.create_index("ix_tools_service_id", "tools", ["service_id"], unique=False)
    op.create_index("uq_tools_service_id_name", "tools", ["service_id", "name"], unique=True)
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("token_id", sa.Uuid(), nullable=True),
        sa.Column("agent_name", sa.String(), nullable=True),
        sa.Column("agent_version", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=True),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("owner_user_id", sa.Uuid(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("skill_version_id", sa.Uuid(), nullable=True),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["orgs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["cam_tokens.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_logs_org_id_owner_user_id_ts",
        "audit_logs",
        ["org_id", "owner_user_id", "ts"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_org_id_service_id_ts",
        "audit_logs",
        ["org_id", "service_id", "ts"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_org_id_user_id_ts", "audit_logs", ["org_id", "user_id", "ts"], unique=False
    )
    op.create_index("ix_audit_logs_ts", "audit_logs", ["ts"], unique=False)

    orgs = sa.table(
        "orgs",
        sa.column("id", sa.Uuid()),
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
        sa.column("settings", postgresql.JSONB()),
    )
    op.bulk_insert(
        orgs,
        [
            {
                "id": DEFAULT_ORG_ID,
                "slug": "indiamart",
                "name": "IndiaMART",
                "settings": {},
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_ts", table_name="audit_logs")
    op.drop_index("ix_audit_logs_org_id_user_id_ts", table_name="audit_logs")
    op.drop_index("ix_audit_logs_org_id_service_id_ts", table_name="audit_logs")
    op.drop_index("ix_audit_logs_org_id_owner_user_id_ts", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("uq_tools_service_id_name", table_name="tools")
    op.drop_index("ix_tools_service_id", table_name="tools")
    op.drop_index("ix_tools_org_id", table_name="tools")
    op.drop_table("tools")
    op.drop_index(
        "uq_connections_service_user",
        table_name="connections",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_connections_service_org_credential",
        table_name="connections",
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.drop_index("ix_connections_user_id", table_name="connections")
    op.drop_index("ix_connections_service_id", table_name="connections")
    op.drop_index("ix_connections_org_id", table_name="connections")
    op.drop_table("connections")
    op.drop_index("ix_url_allowlist_org_id", table_name="url_allowlist")
    op.drop_table("url_allowlist")
    op.drop_index(
        "uq_services_org_slug_global_template",
        table_name="services",
        postgresql_where=sa.text("tier IN ('global', 'template')"),
    )
    op.drop_index(
        "uq_services_org_owner_slug_personal",
        table_name="services",
        postgresql_where=sa.text("tier = 'personal'"),
    )
    op.drop_index("ix_services_owner_user_id", table_name="services")
    op.drop_index("ix_services_org_id", table_name="services")
    op.drop_table("services")
    op.drop_index("ix_login_attempts_org_id", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_index("ix_cam_tokens_user_id", table_name="cam_tokens")
    op.drop_index("ix_cam_tokens_org_id", table_name="cam_tokens")
    op.drop_table("cam_tokens")
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
    op.drop_table("orgs")
