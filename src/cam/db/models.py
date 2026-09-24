import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cam.db.base import Base


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'")
    )
    data_key_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="role_valid"),
        CheckConstraint("kind IN ('person', 'team')", name="kind_valid"),
        CheckConstraint("status IN ('active', 'deactivated')", name="status_valid"),
        UniqueConstraint("org_id", "handle", name="uq_users_org_id_handle"),
        Index("ix_users_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    handle: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="person")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default=text("'active'")
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CamToken(Base):
    __tablename__ = "cam_tokens"
    __table_args__ = (
        Index("ix_cam_tokens_org_id", "org_id"),
        Index("ix_cam_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(8), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("tier IN ('global', 'template', 'personal')", name="tier_valid"),
        CheckConstraint("kind IN ('mcp', 'rest')", name="kind_valid"),
        CheckConstraint(
            "transport IS NULL OR transport IN ('streamable-http', 'sse')",
            name="transport_valid",
        ),
        CheckConstraint(
            "auth_type IN ('none', 'static-header', 'bearer', 'api-key', 'basic', 'oauth')",
            name="auth_type_valid",
        ),
        CheckConstraint("visibility IN ('all', 'restricted')", name="visibility_valid"),
        CheckConstraint("status IN ('active', 'disabled', 'deleted')", name="status_valid"),
        CheckConstraint(
            "health IN ('unknown', 'healthy', 'failing', 'needs_reconnect')",
            name="health_valid",
        ),
        CheckConstraint(
            "(tier = 'personal' AND owner_user_id IS NOT NULL)"
            " OR (tier != 'personal' AND owner_user_id IS NULL)",
            name="owner_by_tier",
        ),
        Index(
            "uq_services_org_slug_global_template",
            "org_id",
            "slug",
            unique=True,
            postgresql_where=text("tier IN ('global', 'template')"),
        ),
        Index(
            "uq_services_org_owner_slug_personal",
            "org_id",
            "owner_user_id",
            "slug",
            unique=True,
            postgresql_where=text("tier = 'personal'"),
        ),
        Index("ix_services_org_id", "org_id"),
        Index("ix_services_owner_user_id", "owner_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    slug: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    kind: Mapped[str] = mapped_column(String, nullable=False, default="mcp")
    url: Mapped[str] = mapped_column(String, nullable=False)
    transport: Mapped[str | None] = mapped_column(String)
    spec_url: Mapped[str | None] = mapped_column(String)
    auth_type: Mapped[str] = mapped_column(String, nullable=False, default="none")
    auth_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'")
    )
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, default="all", server_default=text("'all'")
    )
    timeout_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60000, server_default=text("60000")
    )
    response_cap_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50000, server_default=text("50000")
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default=text("'active'")
    )
    health: Mapped[str] = mapped_column(
        String, nullable=False, default="unknown", server_default=text("'unknown'")
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_secret_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Tool(Base):
    __tablename__ = "tools"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending_review', 'removed_upstream')",
            name="status_valid",
        ),
        Index("uq_tools_service_id_name", "service_id", "name", unique=True),
        Index("ix_tools_org_id", "org_id"),
        Index("ix_tools_service_id", "service_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("services.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    upstream_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    description_override: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str] | None] = mapped_column(JSONB)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rest_binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Connection(Base):
    __tablename__ = "connections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('connected', 'needs_reconnect', 'disabled')",
            name="status_valid",
        ),
        Index(
            "uq_connections_service_user",
            "service_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_connections_service_org_credential",
            "service_id",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        Index("ix_connections_org_id", "org_id"),
        Index("ix_connections_service_id", "service_id"),
        Index("ix_connections_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("services.id"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    secret_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    refresh_enc: Mapped[bytes | None] = mapped_column(nullable=True)
    key_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="connected", server_default=text("'connected'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ServiceVisibility(Base):
    __tablename__ = "service_visibility"
    __table_args__ = (
        UniqueConstraint("service_id", "user_id", name="uq_service_visibility_service_user"),
        Index("ix_service_visibility_org_id", "org_id"),
        Index("ix_service_visibility_service_id", "service_id"),
        Index("ix_service_visibility_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("services.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Share(Base):
    __tablename__ = "shares"
    __table_args__ = (
        Index(
            "uq_shares_active_connection_grantee",
            "connection_id",
            "grantee_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_shares_org_id", "org_id"),
        Index("ix_shares_connection_id", "connection_id"),
        Index("ix_shares_grantee_user_id", "grantee_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("connections.id"), nullable=False
    )
    grantee_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_org_id_user_id_ts", "org_id", "user_id", "ts"),
        Index("ix_audit_logs_org_id_owner_user_id_ts", "org_id", "owner_user_id", "ts"),
        Index("ix_audit_logs_org_id_service_id_ts", "org_id", "service_id", "ts"),
        Index("ix_audit_logs_ts", "ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    token_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("cam_tokens.id"))
    agent_name: Mapped[str | None] = mapped_column(String)
    agent_version: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, nullable=False)
    service_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("services.id"))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("connections.id"))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    tool_name: Mapped[str | None] = mapped_column(String)
    skill_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    args: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(String)
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    response_bytes: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool | None] = mapped_column(Boolean)


class UrlAllowlist(Base):
    __tablename__ = "url_allowlist"
    __table_args__ = (Index("ix_url_allowlist_org_id", "org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    pattern: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"
    __table_args__ = (
        Index("uq_oauth_states_state", "state", unique=True),
        Index("ix_oauth_states_org_id", "org_id"),
        Index("ix_oauth_states_user_id", "user_id"),
        Index("ix_oauth_states_service_id", "service_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("services.id"), nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    pkce_verifier: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint("tier IN ('global', 'personal')", name="tier_valid"),
        CheckConstraint("visibility IN ('all', 'restricted')", name="visibility_valid"),
        CheckConstraint(
            "(tier = 'personal' AND owner_user_id IS NOT NULL)"
            " OR (tier = 'global' AND owner_user_id IS NULL)",
            name="owner_by_tier",
        ),
        Index(
            "uq_skills_org_slug_global",
            "org_id",
            "slug",
            unique=True,
            postgresql_where=text("tier = 'global'"),
        ),
        Index(
            "uq_skills_org_owner_slug_personal",
            "org_id",
            "owner_user_id",
            "slug",
            unique=True,
            postgresql_where=text("tier = 'personal'"),
        ),
        Index("ix_skills_org_id", "org_id"),
        Index("ix_skills_owner_user_id", "owner_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    tier: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    slug: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(
        String, nullable=False, default="all", server_default=text("'all'")
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SkillVersion(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (
        Index("uq_skill_versions_skill_version", "skill_id", "version", unique=True),
        Index("ix_skill_versions_org_id", "org_id"),
        Index("ix_skill_versions_skill_id", "skill_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skills.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SkillFile(Base):
    __tablename__ = "skill_files"
    __table_args__ = (
        Index("uq_skill_files_version_path", "version_id", "path", unique=True),
        Index("ix_skill_files_org_id", "org_id"),
        Index("ix_skill_files_version_id", "version_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skill_versions.id"), nullable=False
    )
    path: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)


class SkillRequires(Base):
    __tablename__ = "skill_requires"
    __table_args__ = (
        Index("uq_skill_requires_skill_service", "skill_id", "service_id", unique=True),
        Index("ix_skill_requires_org_id", "org_id"),
        Index("ix_skill_requires_skill_id", "skill_id"),
        Index("ix_skill_requires_service_id", "service_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skills.id"), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("services.id"), nullable=False)


class SkillVisibility(Base):
    __tablename__ = "skill_visibility"
    __table_args__ = (
        UniqueConstraint("skill_id", "user_id", name="uq_skill_visibility_skill_user"),
        Index("ix_skill_visibility_org_id", "org_id"),
        Index("ix_skill_visibility_skill_id", "skill_id"),
        Index("ix_skill_visibility_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skills.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SkillShare(Base):
    __tablename__ = "skill_shares"
    __table_args__ = (
        Index(
            "uq_skill_shares_active_skill_grantee",
            "skill_id",
            "grantee_user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_skill_shares_org_id", "org_id"),
        Index("ix_skill_shares_skill_id", "skill_id"),
        Index("ix_skill_shares_grantee_user_id", "grantee_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("orgs.id"), nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skills.id"), nullable=False)
    grantee_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_org_id", "org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("orgs.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    email: Mapped[str] = mapped_column(String, nullable=False)
    ip: Mapped[str | None] = mapped_column(String)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
