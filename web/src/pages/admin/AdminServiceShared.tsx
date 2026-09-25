import { useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link } from "react-router-dom"

import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { ApiError, apiFetch } from "@/lib/api"
import type { AdminTestResult, ServiceDetail } from "@/lib/types"

export interface VisibilityGrantee {
  email: string
  handle: string
}

export interface VisibilityState {
  service: string
  visibility: string
  grantees: VisibilityGrantee[]
}

interface AdminServicesResponse {
  services: ServiceDetail[]
}

function healthVariant(health: string): "success" | "destructive" | "warning" | "muted" {
  if (health === "healthy") return "success"
  if (health === "failing") return "destructive"
  if (health === "needs_reconnect") return "warning"
  return "muted"
}

function tierTag(tier: string) {
  if (tier === "global") return <Badge>global</Badge>
  if (tier === "template") return <Badge variant="warning">template</Badge>
  return <Badge variant="muted">{tier}</Badge>
}

export function testResultMessage(result: AdminTestResult): string {
  if (result.ok) {
    const pending =
      result.pending_review_count > 0
        ? ` (${result.pending_review_count} pending review — approve in Review queue)`
        : ""
    return `Connected — ${result.tool_count} tools imported${pending}.`
  }
  const hint = result.hint ? ` ${result.hint}` : ""
  return `Test failed: ${result.error ?? "unknown error"}.${hint}`
}

function PendingBadge({ counts }: { counts: Record<string, number> }) {
  const pending = counts.pending_review ?? 0
  if (pending <= 0) return null
  return (
    <Link to="/admin/review-queue">
      <Badge variant="warning">{pending} pending review</Badge>
    </Link>
  )
}

function VisibilityPanel({ service, onDone }: { service: ServiceDetail; onDone: () => void }) {
  const queryClient = useQueryClient()
  const [email, setEmail] = useState("")
  const [error, setError] = useState<string | null>(null)
  const normalized = service.id

  const query = useQuery({
    queryKey: ["visibility", normalized],
    queryFn: () => apiFetch<VisibilityState>(`/api/admin/services/${normalized}/visibility`),
  })

  const apply = (fn: () => Promise<VisibilityState>) => {
    setError(null)
    fn()
      .then(() => {
        void queryClient.invalidateQueries({ queryKey: ["visibility", normalized] })
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "Could not update visibility.")
      })
  }

  return (
    <div className="space-y-3 rounded-md border p-3">
      {error && <Alert variant="destructive">{error}</Alert>}
      {query.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {query.data && (
        <>
          <div className="flex items-center gap-3">
            <Button
              size="sm"
              variant={query.data.visibility === "all" ? "default" : "outline"}
              onClick={() =>
                apply(() =>
                  apiFetch<VisibilityState>(
                    `/api/admin/services/${normalized}/visibility`,
                    {
                      method: "PATCH",
                      body: { visibility: "all" },
                    },
                  ),
                )
              }
            >
              everyone
            </Button>
            <Button
              size="sm"
              variant={query.data.visibility === "restricted" ? "default" : "outline"}
              onClick={() =>
                apply(() =>
                  apiFetch<VisibilityState>(
                    `/api/admin/services/${normalized}/visibility`,
                    {
                      method: "PATCH",
                      body: { visibility: "restricted" },
                    },
                  ),
                )
              }
            >
              specific users
            </Button>
          </div>
          {query.data.visibility === "restricted" && (
            <div className="space-y-2">
              {query.data.grantees.length > 0 ? (
                <ul className="space-y-1">
                  {query.data.grantees.map((grantee) => (
                    <li key={grantee.email} className="flex items-center gap-2">
                      <span className="text-sm">{grantee.email}</span>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          apply(() =>
                            apiFetch<VisibilityState>(
                              `/api/admin/services/${normalized}/visibility/grant`,
                              {
                                method: "DELETE",
                                body: { email: grantee.email },
                              },
                            ),
                          )
                        }
                      >
                        Remove
                      </Button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">No specific users granted yet.</p>
              )}
              <div className="flex items-center gap-2">
                <Input
                  className="max-w-xs"
                  placeholder="user@example.com"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                />
                <Button
                  size="sm"
                  disabled={!email.trim()}
                  onClick={() =>
                    apply(() =>
                      apiFetch<VisibilityState>(
                        `/api/admin/services/${normalized}/visibility/grant`,
                        {
                          method: "POST",
                          body: { email: email.trim() },
                        },
                      ).then((result) => {
                        setEmail("")
                        return result
                      }),
                    )
                  }
                >
                  Grant
                </Button>
              </div>
            </div>
          )}
        </>
      )}
      <Button size="sm" variant="ghost" onClick={onDone}>
        Close
      </Button>
    </div>
  )
}

export function AdminServiceRows({
  services,
}: {
  services: ServiceDetail[]
}) {
  const queryClient = useQueryClient()
  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [syncingId, setSyncingId] = useState<string | null>(null)
  const [visibilityId, setVisibilityId] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [syncResults, setSyncResults] = useState<Record<string, AdminTestResult>>({})

  if (services.length === 0) {
    return (
      <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
        None yet. Use the form above to register one.
      </p>
    )
  }
  return (
    <div className="space-y-2">
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Ref</TableHead>
              <TableHead>Tier</TableHead>
              <TableHead>Health</TableHead>
              <TableHead>Visibility</TableHead>
              <TableHead>Tools</TableHead>
              <TableHead>Active</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {services.map((service) => (
              <TableRow key={service.id}>
                <TableCell className="font-medium">{service.name}</TableCell>
                <TableCell className="font-mono text-xs">{service.id}</TableCell>
                <TableCell>{tierTag(service.tier)}</TableCell>
                <TableCell>
                  <Badge variant={healthVariant(service.health)}>{service.health}</Badge>
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {service.visibility}
                </TableCell>
                <TableCell className="space-y-1">
                  <span className="text-xs">
                    {(service.tool_counts?.active ?? 0) + (service.tool_counts?.pending_review ?? 0)}{" "}
                    tools
                  </span>
                  {service.tool_counts && <PendingBadge counts={service.tool_counts} />}
                  {service.tier === "template" &&
                    (service.tool_counts?.active ?? 0) +
                      (service.tool_counts?.pending_review ?? 0) ===
                      0 && (
                      <p className="text-xs text-muted-foreground">
                        No tools imported yet — a user must connect first (or use Sync after a user
                        connects)
                      </p>
                    )}
                </TableCell>
                <TableCell>
                  <Switch
                    checked={service.status === "active"}
                    disabled={togglingId === service.id}
                    onCheckedChange={() => {
                      setTogglingId(service.id)
                      apiFetch<{ service: unknown }>(`/api/services/${service.id}`, {
                        method: "PATCH",
                        body: { status: service.status === "active" ? "disabled" : "active" },
                      })
                        .then(() => {
                          setTogglingId(null)
                          void queryClient.invalidateQueries({ queryKey: ["admin-services"] })
                        })
                        .catch((err: unknown) => {
                          setTogglingId(null)
                          setMessage(
                            err instanceof ApiError ? err.message : "Could not update service.",
                          )
                        })
                    }}
                  />
                </TableCell>
                <TableCell className="space-x-2 text-right whitespace-nowrap">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={syncingId === service.id}
                    onClick={() => {
                      setSyncingId(service.id)
                      setSyncResults((prev) => ({
                        ...prev,
                        [service.id]: undefined as unknown as AdminTestResult,
                      }))
                      apiFetch<{ service: string; test: AdminTestResult }>(
                        `/api/admin/services/${service.id}/resync`,
                        { method: "POST", body: {} },
                      )
                        .then((res) => {
                          setSyncingId(null)
                          setSyncResults((prev) => ({ ...prev, [service.id]: res.test }))
                          void queryClient.invalidateQueries({ queryKey: ["admin-services"] })
                        })
                        .catch((err: unknown) => {
                          setSyncingId(null)
                          setMessage(
                            err instanceof ApiError
                              ? err.message
                              : "Could not test/sync the service.",
                          )
                        })
                    }}
                  >
                    {syncingId === service.id ? "Syncing…" : "Test/Sync"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setVisibilityId(service.id)}
                  >
                    Visibility
                  </Button>
                  <Button size="sm" variant="outline" asChild>
                    <Link to={`/services/${service.id}`}>Open</Link>
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {services.some(
        (service) => syncResults[service.id] !== undefined,
      ) &&
        services.map(
          (service) =>
            syncResults[service.id] !== undefined && (
              <Alert
                key={service.id}
                variant={syncResults[service.id].ok ? "default" : "destructive"}
              >
                <span className="font-medium">{service.id}:</span>{" "}
                {testResultMessage(syncResults[service.id])}
              </Alert>
            ),
        )}
      {message && <p className="text-sm text-destructive">{message}</p>}
      {visibilityId && (
        <VisibilityPanel
          service={services.find((service) => service.id === visibilityId)!}
          onDone={() => setVisibilityId(null)}
        />
      )}
    </div>
  )
}

interface RegisterDraft {
  tier: string
  kind: string
  slug: string
  name: string
  url: string
  specUrl: string
  transport: string
  description: string
  authType: string
  headerName: string
  credential: string
  username: string
  timeoutMs: string
  responseCap: string
}

const initialDraft: RegisterDraft = {
  tier: "global",
  kind: "mcp",
  slug: "",
  name: "",
  url: "",
  specUrl: "",
  transport: "streamable-http",
  description: "",
  authType: "none",
  headerName: "X-Api-Key",
  credential: "",
  username: "",
  timeoutMs: "60000",
  responseCap: "50000",
}

export function RegisterGlobalServiceForm() {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<RegisterDraft>(initialDraft)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const set = <K extends keyof RegisterDraft>(key: K, value: RegisterDraft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }))

  const requestBody = () => ({
    tier: draft.tier,
    slug: draft.slug,
    name: draft.name,
    url: draft.url,
    kind: draft.kind,
    transport: draft.kind === "mcp" ? draft.transport : null,
    spec_url: draft.kind === "rest" && draft.specUrl ? draft.specUrl : null,
    auth_type: draft.authType,
    header_name: draft.authType === "static-header" ? draft.headerName : null,
    credential: draft.authType === "none" ? null : draft.credential,
    username: draft.authType === "basic" ? draft.username : null,
    description: draft.description,
    timeout_ms: Number(draft.timeoutMs),
    response_cap_tokens: Number(draft.responseCap),
  })

  const submit = () => {
    if (!/^[a-z0-9][a-z0-9-]*$/.test(draft.slug))
      return setMessage("Slug must be lowercase letters, digits and hyphens.")
    if (!draft.name.trim()) return setMessage("Name is required.")
    if (!/^https?:\/\//.test(draft.url)) return setMessage("URL must start with http(s)://.")
    if (draft.authType !== "none" && !draft.credential)
      return setMessage("A credential is required for this auth type.")
    setSaving(true)
    setMessage(null)
    apiFetch<{ service: ServiceDetail; test?: AdminTestResult }>("/api/admin/services", {
      method: "POST",
      body: requestBody(),
    })
      .then((created) => {
        setSaving(false)
        setDraft(initialDraft)
        void queryClient.invalidateQueries({ queryKey: ["admin-services"] })
        void queryClient.invalidateQueries({ queryKey: ["services"] })
        setMessage(
          created.test
            ? `Registered ${created.service.id}. ${testResultMessage(created.test)}`
            : `Registered ${created.service.id}.`,
        )
      })
      .catch((err: unknown) => {
        setSaving(false)
        setMessage(err instanceof ApiError ? err.message : "Could not register service.")
      })
  }

  return (
    <div className="space-y-3 rounded-md border p-4">
      <div>
        <h2 className="font-medium">Register org service</h2>
        <p className="text-sm text-muted-foreground">
          Registers a global or template service with the org credential. Visibility can
          be changed per service below.
        </p>
      </div>
      {message && <div className="rounded-md border p-3 text-sm">{message}</div>}
      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor="admin-svc-tier">Tier</Label>
          <Select value={draft.tier} onValueChange={(value) => set("tier", value)}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="global">global: everyone can invoke with the org credential</SelectItem>
              <SelectItem value="template">template: each user connects their own account</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="admin-svc-kind">Kind</Label>
          <Select value={draft.kind} onValueChange={(value) => set("kind", value)}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="mcp">MCP server</SelectItem>
              <SelectItem value="rest">REST API</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {draft.kind === "mcp" && (
          <div className="space-y-1">
            <Label htmlFor="admin-svc-transport">Transport</Label>
            <Select
              value={draft.transport}
              onValueChange={(value) => set("transport", value)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="streamable-http">streamable-http</SelectItem>
                <SelectItem value="sse">sse</SelectItem>
              </SelectContent>
            </Select>
          </div>
        )}
        {draft.kind === "rest" && (
          <div className="space-y-1">
            <Label htmlFor="admin-svc-spec">OpenAPI spec URL (optional)</Label>
            <Input
              id="admin-svc-spec"
              value={draft.specUrl}
              onChange={(event) => set("specUrl", event.target.value)}
            />
          </div>
        )}
        <div className="space-y-1">
          <Label htmlFor="admin-svc-slug">Slug</Label>
          <Input
            id="admin-svc-slug"
            placeholder="redash"
            value={draft.slug}
            onChange={(event) => set("slug", event.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="admin-svc-name">Name</Label>
          <Input
            id="admin-svc-name"
            placeholder="Redash"
            value={draft.name}
            onChange={(event) => set("name", event.target.value)}
          />
        </div>
        <div className="space-y-1 md:col-span-2">
          <Label htmlFor="admin-svc-url">URL</Label>
          <Input
            id="admin-svc-url"
            placeholder="https://mcp.example.com/mcp"
            value={draft.url}
            onChange={(event) => set("url", event.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="admin-svc-auth">Auth type</Label>
          <Select value={draft.authType} onValueChange={(value) => set("authType", value)}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">none</SelectItem>
              <SelectItem value="static-header">static-header</SelectItem>
              <SelectItem value="bearer">bearer</SelectItem>
              <SelectItem value="api-key">api-key</SelectItem>
              <SelectItem value="basic">basic</SelectItem>
              <SelectItem value="oauth">oauth</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {draft.authType === "static-header" && (
          <div className="space-y-1">
            <Label htmlFor="admin-svc-header">Header name</Label>
            <Input
              id="admin-svc-header"
              value={draft.headerName}
              onChange={(event) => set("headerName", event.target.value)}
            />
          </div>
        )}
        {draft.authType === "basic" && (
          <div className="space-y-1">
            <Label htmlFor="admin-svc-username">Username</Label>
            <Input
              id="admin-svc-username"
              value={draft.username}
              onChange={(event) => set("username", event.target.value)}
            />
          </div>
        )}
        {draft.authType !== "none" && (
          <div className="space-y-1 md:col-span-2">
            <Label htmlFor="admin-svc-cred">
              {draft.authType === "basic" ? "Password" : "Credential"}
            </Label>
            <Input
              id="admin-svc-cred"
              type="password"
              value={draft.credential}
              onChange={(event) => set("credential", event.target.value)}
            />
            {draft.authType !== "basic" &&
              draft.authType !== "bearer" &&
              draft.authType !== "oauth" && (
                <p className="text-xs text-muted-foreground">
                  Full header value, exactly as the upstream expects it — include any prefix
                  such as `Bearer ` if the upstream sends one.
                </p>
              )}
            {draft.authType === "bearer" && (
              <p className="text-xs text-muted-foreground">
                The value is sent as `Authorization: Bearer &lt;value&gt;`.
              </p>
            )}
            {draft.authType === "basic" && (
              <p className="text-xs text-muted-foreground">
                The password is combined with the username and sent as
                `Authorization: Basic &lt;base64 of user:password&gt;`.
              </p>
            )}
          </div>
        )}
        <div className="space-y-1">
          <Label htmlFor="admin-svc-timeout">Timeout (ms)</Label>
          <Input
            id="admin-svc-timeout"
            inputMode="numeric"
            value={draft.timeoutMs}
            onChange={(event) => set("timeoutMs", event.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="admin-svc-cap">Response cap (tokens)</Label>
          <Input
            id="admin-svc-cap"
            inputMode="numeric"
            value={draft.responseCap}
            onChange={(event) => set("responseCap", event.target.value)}
          />
        </div>
        <div className="space-y-1 md:col-span-2">
          <Label htmlFor="admin-svc-desc">Description</Label>
          <Textarea
            id="admin-svc-desc"
            rows={2}
            value={draft.description}
            onChange={(event) => set("description", event.target.value)}
          />
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Button disabled={saving} onClick={submit}>
          {saving ? "Registering…" : "Register service"}
        </Button>
      </div>
    </div>
  )
}

export function useAdminServices() {
  return useQuery({
    queryKey: ["admin-services"],
    queryFn: () => apiFetch<AdminServicesResponse>("/api/admin/services"),
  })
}
