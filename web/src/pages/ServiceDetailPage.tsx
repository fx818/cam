import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useNavigate, useParams } from "react-router-dom"

import { Alert } from "@/components/ui/alert"
import { ConnectDialog } from "@/components/ConnectDialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { ApiError, apiFetch } from "@/lib/api"
import type {
  AuditEntry,
  AuditListResponse,
  ServiceDetailResponse,
  ShareRow,
  SharesResponse,
  ToolRow,
  ToolsResponse,
} from "@/lib/types"

function healthVariant(health: string): "success" | "destructive" | "warning" | "muted" {
  if (health === "healthy") return "success"
  if (health === "failing") return "destructive"
  if (health === "needs_reconnect") return "warning"
  return "muted"
}

function ToolsTab({
  refPath,
  canManage,
}: {
  refPath: string
  canManage: boolean
}) {
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<ToolRow | null>(null)
  const [description, setDescription] = useState("")
  const [usageNote, setUsageNote] = useState("")
  const [example, setExample] = useState("")
  const [tryArgs, setTryArgs] = useState("")
  const [tryToolId, setTryToolId] = useState<string | null>(null)
  const [tryResult, setTryResult] = useState<string | null>(null)
  const [trying, setTrying] = useState(false)
  const [status, setStatus] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ["tools", refPath],
    queryFn: () => apiFetch<ToolsResponse>(`/api/services/${refPath}/tools`),
  })

  const patchMutation = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Record<string, unknown> }) =>
      apiFetch<{ ok: boolean }>(`/api/tools/${id}`, {
        method: "PATCH",
        body: patch,
      }),
    onSuccess: () => {
      setEditing(null)
      void queryClient.invalidateQueries({ queryKey: ["tools", refPath] })
    },
    onError: (err) => {
      setStatus(err instanceof ApiError ? err.message : "Could not update tool.")
    },
  })

  const testMutation = useMutation({
    mutationFn: ({ id, args }: { id: string; args: Record<string, unknown> }) =>
      apiFetch<{ invoke: { is_error: boolean; text: string } }>(
        `/api/admin/tools/${id}/invoke`,
        { method: "POST", body: { args } },
      ),
    onError: (err) => {
      setTryResult(err instanceof ApiError ? err.message : "Could not invoke tool.")
      setTrying(false)
    },
    onSuccess: (res) => {
      setTrying(false)
      setTryResult(`${res.invoke.is_error ? "Error" : "OK"}: ${res.invoke.text.slice(0, 4000)}`)
    },
  })

  const runTry = () => {
    if (tryToolId === null) return
    let args: Record<string, unknown> = {}
    try {
      args = tryArgs.trim() ? (JSON.parse(tryArgs) as Record<string, unknown>) : {}
    } catch {
      setTryResult("Arguments must be valid JSON.")
      return
    }
    setTrying(true)
    setTryResult(null)
    testMutation.mutate({ id: tryToolId, args })
  }

  const resyncMutation = useMutation({
    mutationFn: () =>
      apiFetch<unknown>(`/api/services/${refPath}/resync`, { method: "POST" }),
    onSuccess: () => {
      setStatus("Tool list re-synced.")
      void queryClient.invalidateQueries({ queryKey: ["tools", refPath] })
    },
    onError: (err) => {
      setStatus(err instanceof ApiError ? err.message : "Could not re-sync tools.")
    },
  })

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>
  if (error)
    return (
      <p className="text-sm text-destructive">
        {error instanceof ApiError ? error.message : "Could not load tools."}
      </p>
    )
  if (!data || data.tools.length === 0)
    return (
      <div className="space-y-1">
        <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
          No tools discovered yet. Tools appear after a successful test or sync.
        </p>
        <p className="text-xs text-muted-foreground">
          Awaiting import/approval — new template tools are approved by an admin in the Review
          queue.
        </p>
      </div>
    )

  return (
    <div className="space-y-3">
      {status && <p className="text-sm text-muted-foreground">{status}</p>}
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data.tools.length} tools · shared viewers see enabled tools only
        </p>
        {canManage && (
          <Button
            size="sm"
            variant="outline"
            disabled={resyncMutation.isPending}
            onClick={() => resyncMutation.mutate()}
          >
            Re-sync tools
          </Button>
        )}
      </div>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Description</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Enabled</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.tools.map((tool) => (
              <TableRow key={tool.id}>
                <TableCell className="font-medium">{tool.name}</TableCell>
                <TableCell className="max-w-md truncate text-xs text-muted-foreground">
                  {tool.description_override ?? tool.description ?? "—"}
                </TableCell>
                <TableCell>
                  <Badge variant={tool.status === "error" ? "destructive" : "success"}>
                    {tool.status}
                  </Badge>
                </TableCell>
                <TableCell>
                  <Switch
                    checked={tool.enabled}
                    disabled={!canManage || patchMutation.isPending}
                    onCheckedChange={(checked) =>
                      patchMutation.mutate({ id: tool.id, patch: { enabled: checked } })
                    }
                  />
                </TableCell>
                <TableCell className="text-right">
                  <span className="inline-flex gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        setTryToolId(tool.id)
                        setTryArgs(
                          tool.example_invocation
                            ? JSON.stringify(tool.example_invocation, null, 2)
                            : "",
                        )
                        setTryResult(null)
                      }}
                      title="Run this tool as yourself"
                    >
                      Try it
                    </Button>
                    {canManage && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setDescription(tool.description_override ?? tool.description ?? "")
                          setUsageNote(tool.usage_note ?? "")
                          setExample(
                            tool.example_invocation
                              ? JSON.stringify(tool.example_invocation, null, 2)
                              : "",
                          )
                          setEditing(tool)
                        }}
                      >
                        Edit
                      </Button>
                    )}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <Dialog open={editing !== null} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Edit tool · {editing?.name}</DialogTitle>
            <DialogDescription>
              Override what agents see and how they should call this tool.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="tool-desc">Description</Label>
            <Textarea
              id="tool-desc"
              rows={4}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
            <Label htmlFor="tool-usage">Usage note (returned to agents in describe)</Label>
            <Textarea
              id="tool-usage"
              rows={3}
              placeholder="e.g. dates are YYYY-MM-DD IST; use dept_id from user_info"
              value={usageNote}
              onChange={(event) => setUsageNote(event.target.value)}
            />
            <Label htmlFor="tool-example">Example invocation (JSON args; blank = auto)</Label>
            <Textarea
              id="tool-example"
              rows={4}
              placeholder='{ "dept_id": 4, "date": "2026-01-01" }'
              value={example}
              onChange={(event) => setExample(event.target.value)}
            />
            {example.trim() && (() => {
              try {
                JSON.parse(example)
              } catch {
                return <p className="text-xs text-destructive">Example is not valid JSON.</p>
              }
              return null
            })()}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button
              disabled={patchMutation.isPending}
              onClick={() => {
                if (!editing) return
                let parsed: Record<string, unknown> | null = null
                if (example.trim()) {
                  try {
                    parsed = JSON.parse(example) as Record<string, unknown>
                  } catch {
                    setStatus("Example is not valid JSON.")
                    return
                  }
                }
                patchMutation.mutate({
                  id: editing.id,
                  patch: {
                    description_override: description,
                    usage_note: usageNote,
                    example_invocation: parsed,
                  },
                })
              }}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={tryToolId !== null || trying}
        onOpenChange={(open) => {
          if (!open) {
            setTryResult(null)
            setTrying(false)
            setTryToolId(null)
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Try tool</DialogTitle>
            <DialogDescription>
              Runs the real pipeline as your account (audited like an agent call).
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="try-args">Arguments (JSON)</Label>
            <Textarea
              id="try-args"
              rows={6}
              value={tryArgs}
              onChange={(event) => setTryArgs(event.target.value)}
            />
            {tryResult && (
              <pre className="max-h-64 overflow-auto rounded-md border bg-muted p-2 text-xs">
                {tryResult}
              </pre>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setTryResult(null)
                setTrying(false)
                setTryToolId(null)
              }}
            >
              Close
            </Button>
            <Button
              disabled={trying || tryToolId === null}
              onClick={() => {
                let args: Record<string, unknown> = {}
                try {
                  args = tryArgs.trim() ? (JSON.parse(tryArgs) as Record<string, unknown>) : {}
                } catch {
                  setTryResult("Arguments must be valid JSON.")
                  return
                }
                if (typeof args !== "object" || Array.isArray(args) || args === null) {
                  setTryResult("Arguments must be a JSON object.")
                  return
                }
                runTry()
              }}
            >
              Run
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function ConnectionTab({
  refPath,
  connection,
  authType,
}: {
  refPath: string
  connection: { id: string; status: string; expires_at: string | null } | null
  authType: string
}) {
  const queryClient = useQueryClient()
  const [rotateOpen, setRotateOpen] = useState(false)
  const [secret, setSecret] = useState("")
  const [message, setMessage] = useState<string | null>(null)

  const rotateMutation = useMutation({
    mutationFn: (value: string) =>
      apiFetch<{ ok: boolean }>(`/api/connections/${connection?.id}/rotate`, {
        method: "POST",
        body: { secret: value },
      }),
    onSuccess: () => {
      setRotateOpen(false)
      setSecret("")
      setMessage("Secret rotated.")
      void queryClient.invalidateQueries({ queryKey: ["service", refPath] })
    },
    onError: (err) => {
      setMessage(err instanceof ApiError ? err.message : "Could not rotate secret.")
    },
  })

  if (!connection) {
    return (
      <div className="space-y-3">
        <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
          No connection yet. Test the service or connect a template to create one.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {message && <p className="text-sm text-muted-foreground">{message}</p>}
      <dl className="grid max-w-md grid-cols-3 gap-2 text-sm">
        <dt className="text-muted-foreground">Status</dt>
        <dd className="col-span-2">
          <Badge variant={connection.status === "active" ? "success" : "warning"}>
            {connection.status}
          </Badge>
        </dd>
        <dt className="text-muted-foreground">Expires</dt>
        <dd className="col-span-2">
          {connection.expires_at
            ? new Date(connection.expires_at).toLocaleString()
            : "never"}
        </dd>
      </dl>
      {authType === "oauth" && (
        <p className="text-xs text-muted-foreground">
          OAuth reconnect is handled by Phase 3 connect routes; use Reconnect on the
          service if the session expired.
        </p>
      )}
      <Button variant="outline" onClick={() => { setMessage(null); setRotateOpen(true) }}>
        Rotate secret
      </Button>

      <Dialog open={rotateOpen} onOpenChange={setRotateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rotate secret</DialogTitle>
            <DialogDescription>
              The old secret stops working immediately. Paste the new one.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="rotate-secret">New secret</Label>
            <Input
              id="rotate-secret"
              type="password"
              value={secret}
              onChange={(event) => setSecret(event.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRotateOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={rotateMutation.isPending || secret.length === 0}
              onClick={() => rotateMutation.mutate(secret)}
            >
              Rotate
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function SharingTab({
  connectionId,
}: {
  connectionId: string | null
}) {
  const queryClient = useQueryClient()
  const [email, setEmail] = useState("")
  const [message, setMessage] = useState<string | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<ShareRow | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ["shares", connectionId],
    enabled: connectionId !== null,
    queryFn: () =>
      apiFetch<SharesResponse>(`/api/connections/${connectionId}/shares`),
  })

  const addMutation = useMutation({
    mutationFn: (shareEmail: string) =>
      apiFetch<ShareRow>(`/api/connections/${connectionId}/shares`, {
        method: "POST",
        body: { email: shareEmail },
      }),
    onSuccess: () => {
      setEmail("")
      setMessage(null)
      void queryClient.invalidateQueries({ queryKey: ["shares", connectionId] })
    },
    onError: (err) => {
      setMessage(err instanceof ApiError ? err.message : "Could not add share.")
    },
  })

  const revokeMutation = useMutation({
    mutationFn: (shareId: string) =>
      apiFetch<{ ok: boolean }>(`/api/shares/${shareId}`, { method: "DELETE" }),
    onSuccess: () => {
      setRevokeTarget(null)
      void queryClient.invalidateQueries({ queryKey: ["shares", connectionId] })
    },
  })

  if (!connectionId) {
    return (
      <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
        Sharing needs a connection. Connect the service first.
      </p>
    )
  }

  const shares = data?.shares.filter((s) => s.revoked_at === null) ?? []

  return (
    <div className="space-y-4">
      <Alert>
        Shares are all-or-nothing: the person can use every tool on this connection.
        Revoke at any time.
      </Alert>
      {message && <p className="text-sm text-destructive">{message}</p>}
      <div className="flex items-end gap-2">
        <div className="flex-1 space-y-1">
          <Label htmlFor="share-email">Share with (email)</Label>
          <Input
            id="share-email"
            type="email"
            placeholder="teammate@company.com"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </div>
        <Button
          disabled={addMutation.isPending || email.trim().length === 0}
          onClick={() => addMutation.mutate(email.trim())}
        >
          Add
        </Button>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load shares."}
        </p>
      )}
      {data && shares.length === 0 && (
        <p className="text-sm text-muted-foreground">Not shared with anyone yet.</p>
      )}
      {shares.length > 0 && (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Shared</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {shares.map((share) => (
                <TableRow key={share.id}>
                  <TableCell className="text-sm">
                    {share.grantee.email ?? "—"}
                  </TableCell>
                  <TableCell className="text-sm">
                    {share.grantee.display_name ?? "—"}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {share.created_at
                      ? new Date(share.created_at).toLocaleDateString()
                      : "—"}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setRevokeTarget(share)}
                    >
                      Revoke
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog
        open={revokeTarget !== null}
        onOpenChange={(open) => !open && setRevokeTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke access?</DialogTitle>
            <DialogDescription>
              {revokeTarget?.grantee.email} will lose access to this service immediately.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRevokeTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={revokeMutation.isPending}
              onClick={() => revokeTarget && revokeMutation.mutate(revokeTarget.id)}
            >
              Revoke
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function ActivityTab({
  refPath,
  isOwner,
}: {
  refPath: string
  isOwner: boolean
}) {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const load = async () => {
    try {
      const response = await apiFetch<AuditListResponse>(
        `/api/audit?scope=${isOwner ? "my_services" : "mine"}&service=${encodeURIComponent(
          refPath,
        )}&limit=25`,
      )
      setEntries(response.entries)
      setMessage(null)
    } catch (err) {
      setMessage(err instanceof ApiError ? err.message : "Could not load activity.")
    }
  }

  if (entries === null) {
    return (
      <div className="space-y-3">
        {message && <p className="text-sm text-destructive">{message}</p>}
        <Button size="sm" variant="outline" onClick={() => void load()}>
          Load activity
        </Button>
      </div>
    )
  }

  if (entries.length === 0)
    return (
      <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
        No activity on this service yet.
      </p>
    )

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Time</TableHead>
            <TableHead>Actor</TableHead>
            <TableHead>Tool</TableHead>
            <TableHead>Status</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {entries.map((entry) => (
            <TableRow key={entry.id}>
              <TableCell className="whitespace-nowrap text-xs">
                {entry.ts ? new Date(entry.ts).toLocaleString() : "—"}
              </TableCell>
              <TableCell className="text-xs">{entry.actor_email ?? "—"}</TableCell>
              <TableCell className="text-xs">{entry.tool_name ?? "—"}</TableCell>
              <TableCell>
                <Badge variant={entry.status === "error" ? "destructive" : "success"}>
                  {entry.status ?? "unknown"}
                </Badge>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function SettingsTab({
  refPath,
  canManage,
}: {
  refPath: string
  canManage: boolean
}) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: ["service", refPath],
    queryFn: () => apiFetch<ServiceDetailResponse>(`/api/services/${refPath}`),
  })
  const [name, setName] = useState<string | null>(null)
  const [description, setDescription] = useState<string | null>(null)
  const [timeout, setTimeout] = useState<string | null>(null)
  const [cap, setCap] = useState<string | null>(null)
  const [serviceStatus, setServiceStatus] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)

  const service = data?.service
  const patchMutation = useMutation({
    mutationFn: () =>
      apiFetch<{ ok: boolean }>(`/api/services/${refPath}`, {
        method: "PATCH",
        body: {
          ...(name !== null ? { name } : {}),
          ...(description !== null ? { description } : {}),
          ...(timeout !== null ? { timeout_ms: Number(timeout) } : {}),
          ...(cap !== null ? { response_cap_tokens: Number(cap) } : {}),
          ...(serviceStatus !== null ? { status: serviceStatus } : {}),
        },
      }),
    onSuccess: () => {
      setMessage("Saved.")
      void queryClient.invalidateQueries({ queryKey: ["service", refPath] })
    },
    onError: (err) => {
      setMessage(err instanceof ApiError ? err.message : "Could not save settings.")
    },
  })

  const deleteMutation = useMutation({
    mutationFn: () =>
      apiFetch<{ ok: boolean }>(`/api/services/${refPath}`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["services"] })
      navigate("/")
    },
    onError: (err) => {
      setMessage(err instanceof ApiError ? err.message : "Could not delete service.")
      setDeleteOpen(false)
    },
  })

  if (!service) return null
  if (!canManage)
    return (
      <p className="text-sm text-muted-foreground">
        Only the owner or an admin can change these settings.
      </p>
    )

  return (
    <div className="space-y-6">
      {message && <p className="text-sm text-muted-foreground">{message}</p>}
      <Card>
        <CardHeader>
          <CardTitle>Settings</CardTitle>
          <CardDescription>Defaults for requests through this service.</CardDescription>
        </CardHeader>
        <CardContent className="grid max-w-xl gap-4">
          <div className="space-y-1">
            <Label htmlFor="svc-name">Name</Label>
            <Input
              id="svc-name"
              value={name ?? service.name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="svc-desc">Description</Label>
            <Textarea
              id="svc-desc"
              rows={3}
              value={description ?? service.description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1">
              <Label htmlFor="svc-timeout">Timeout (ms)</Label>
              <Input
                id="svc-timeout"
                inputMode="numeric"
                value={timeout ?? String(service.timeout_ms)}
                onChange={(event) => setTimeout(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="svc-cap">Response cap (tokens)</Label>
              <Input
                id="svc-cap"
                inputMode="numeric"
                value={cap ?? String(service.response_cap_tokens)}
                onChange={(event) => setCap(event.target.value)}
              />
            </div>
          </div>
          <div className="space-y-1">
            <Label>Status</Label>
            <Select
              value={serviceStatus ?? service.status}
              onValueChange={setServiceStatus}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="active">Active</SelectItem>
                <SelectItem value="disabled">Disabled</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div>
            <Button disabled={patchMutation.isPending} onClick={() => patchMutation.mutate()}>
              Save changes
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card className="border-destructive/50">
        <CardHeader>
          <CardTitle className="text-destructive">Danger zone</CardTitle>
          <CardDescription>
            Deleting soft-deletes the service, disables its connections and revokes shares.
            Audit history is kept.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="destructive" onClick={() => setDeleteOpen(true)}>
            Delete service
          </Button>
        </CardContent>
      </Card>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete “{service.name}”?</DialogTitle>
            <DialogDescription>
              Agents lose access immediately. Shared users are removed. This cannot be
              undone from the dashboard.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export function ServiceDetailPage() {
  const { owner = "", slug = "" } = useParams()
  const refPath = `${owner}/${slug}`
  const [connectOpen, setConnectOpen] = useState(false)

  const { data, isLoading, error } = useQuery({
    queryKey: ["service", refPath],
    queryFn: () => apiFetch<ServiceDetailResponse>(`/api/services/${refPath}`),
  })

  if (isLoading)
    return <p className="p-6 text-sm text-muted-foreground">Loading…</p>
  if (error)
    return (
      <p className="p-6 text-sm text-destructive">
        {error instanceof ApiError ? error.message : "Could not load service."}
      </p>
    )
  if (!data) return null

  const { service, connection, viewer } = data

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold">{service.name}</h1>
            <Badge variant={healthVariant(service.health)}>{service.health}</Badge>
            <Badge variant="outline">{service.tier}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            {service.id} · {service.url}
          </p>
        </div>
        <div className="flex gap-2">
          {viewer.is_owner && !connection && (
            <Button variant="outline" onClick={() => setConnectOpen(true)}>
              Connect
            </Button>
          )}
        </div>
      </div>

      {service.description && (
        <p className="max-w-3xl text-sm text-muted-foreground">{service.description}</p>
      )}

      <Tabs defaultValue="tools">
        <TabsList>
          <TabsTrigger value="tools">Tools</TabsTrigger>
          {viewer.can_manage && (
            <TabsTrigger value="connection">Connection</TabsTrigger>
          )}
          {viewer.is_owner && (
            <TabsTrigger value="sharing">Sharing</TabsTrigger>
          )}
          {viewer.can_manage && (
            <TabsTrigger value="activity">Activity</TabsTrigger>
          )}
          {viewer.can_manage && <TabsTrigger value="settings">Settings</TabsTrigger>}
        </TabsList>
        <TabsContent value="tools" className="mt-4">
          <ToolsTab refPath={refPath} canManage={viewer.can_manage} />
        </TabsContent>
        <TabsContent value="connection" className="mt-4">
          <ConnectionTab
            refPath={refPath}
            connection={connection}
            authType={service.auth_type}
          />
        </TabsContent>
        <TabsContent value="sharing" className="mt-4">
          <SharingTab connectionId={connection?.id ?? null} />
        </TabsContent>
        <TabsContent value="activity" className="mt-4">
          <ActivityTab refPath={refPath} isOwner={viewer.is_owner} />
        </TabsContent>
        <TabsContent value="settings" className="mt-4">
          <SettingsTab refPath={refPath} canManage={viewer.can_manage} />
        </TabsContent>
      </Tabs>

      {viewer.is_owner && !connection && (
        <ConnectDialog
          target={{ id: refPath, name: service.name }}
          open={connectOpen}
          onOpenChange={setConnectOpen}
          detail={{ auth_type: service.auth_type, auth_config: service.auth_config }}
        />
      )}
    </div>
  )
}
