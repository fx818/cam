export type ServiceGroup = "global" | "templates" | "mine" | "shared"

export interface ConnectionSummary {
  id: string
  status: string
  expires_at: string | null
  created_at: string | null
}

export interface DashboardService {
  id: string
  group: ServiceGroup
  tier: string
  name: string
  description: string
  owner: string | null
  shared_by: string | null
  health: string
  tool_count: number
  status: string
  connection: ConnectionSummary | null
}

export interface ServicesListResponse {
  services: DashboardService[]
}

export interface ServiceDetail {
  id: string
  tier: string
  name: string
  description: string
  slug: string
  kind: string
  url: string
  transport: string | null
  spec_url: string | null
  auth_type: string
  auth_config: Record<string, unknown>
  visibility: string
  timeout_ms: number
  response_cap_tokens: number
  status: string
  health: string
  last_synced_at: string | null
  created_at: string | null
  owner: string | null
}

export interface ViewerInfo {
  is_owner: boolean
  is_admin: boolean
  can_manage: boolean
  is_shared: boolean
}

export interface ServiceDetailResponse {
  service: ServiceDetail
  connection: ConnectionSummary | null
  viewer: ViewerInfo
}

export interface ToolRow {
  id: string
  name: string
  upstream_name: string
  description: string | null
  description_override: string | null
  tags: string[] | null
  enabled: boolean
  status: string
}

export interface ToolsResponse {
  tools: ToolRow[]
  can_manage: boolean
}

export interface TokenRow {
  id: string
  label: string
  token_prefix: string
  last_used_at: string | null
  revoked_at: string | null
  created_at: string | null
  token?: string
}

export interface TokensResponse {
  tokens: TokenRow[]
  next_cursor: string | null
}

export interface ShareGrantee {
  id: string
  email: string | null
  display_name: string | null
}

export interface ShareRow {
  id: string
  created_at: string | null
  revoked_at: string | null
  grantee: ShareGrantee
}

export interface SharesResponse {
  shares: ShareRow[]
}

export interface AuditEntry {
  id: string
  ts: string | null
  action: string
  agent_name: string | null
  agent_version: string | null
  tool_name: string | null
  status: string | null
  error_code: string | null
  error_message: string | null
  latency_ms: number | null
  response_bytes: number | null
  truncated: boolean | null
  service_ref: string | null
  actor_email: string | null
  token_label: string | null
  args?: Record<string, unknown> | null
}

export interface AuditListResponse {
  entries: AuditEntry[]
  next_cursor: string | null
}

export interface SkillsResponse {
  skills: unknown[]
  next_cursor: string | null
}

export interface AdminUser {
  id: string
  email: string
  handle: string
  display_name: string
  role: string
  kind: string
  status: string
  created_at: string | null
  deactivated_at: string | null
}

export interface AdminUsersResponse {
  users: AdminUser[]
}

export interface AdminSettings {
  default_timeout_ms: number
  response_cap_tokens: number
  log_retention_days: number
  allowed_email_domains: string[]
  instructions: string
  brand: Record<string, string>
}

export interface AdminSettingsResponse {
  settings: AdminSettings
}

export interface AllowlistEntry {
  id: string
  pattern: string
  note: string | null
  created_by_email: string | null
  created_at: string | null
}

export interface AllowlistResponse {
  entries: AllowlistEntry[]
}

export interface OverviewResponse {
  users: {
    total: number
    active: number
    deactivated: number
    admins: number
  }
  services: {
    by_tier: Record<string, number>
    by_status: Record<string, number>
    by_health: Record<string, number>
  }
  recent_audit: AuditEntry[]
}
