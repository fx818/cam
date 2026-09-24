import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ApiError, apiFetch } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import type { TokenRow, TokensResponse } from "@/lib/types"

function CopyRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <Label className="text-xs text-muted-foreground">{label}</Label>
        <Button size="sm" variant="ghost" onClick={() => void copy()}>
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">{value}</pre>
    </div>
  )
}

export function AgentSetupPage() {
  const { baseUrl } = useAuth()
  const queryClient = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [label, setLabel] = useState("")
  const [formError, setFormError] = useState<string | null>(null)
  const [rawToken, setRawToken] = useState<string | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<TokenRow | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ["tokens"],
    queryFn: () => apiFetch<TokensResponse>("/api/tokens"),
  })

  const createMutation = useMutation({
    mutationFn: (tokenLabel: string) =>
      apiFetch<TokenRow>("/api/tokens", { method: "POST", body: { label: tokenLabel } }),
    onSuccess: (created) => {
      setCreateOpen(false)
      setLabel("")
      setFormError(null)
      setRawToken(created.token ?? null)
      void queryClient.invalidateQueries({ queryKey: ["tokens"] })
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.message : "Could not create token.")
    },
  })

  const revokeMutation = useMutation({
    mutationFn: (tokenId: string) =>
      apiFetch<{ ok: boolean }>(`/api/tokens/${tokenId}`, { method: "DELETE" }),
    onSuccess: () => {
      setRevokeTarget(null)
      void queryClient.invalidateQueries({ queryKey: ["tokens"] })
    },
  })

  const tokens = data?.tokens ?? []
  const activeTokens = tokens.filter((t) => t.revoked_at === null)
  const sampleToken = rawToken ?? (activeTokens[0] ? `cam_…${activeTokens[0].token_prefix}` : "cam_<your-token>")
  const mcpUrl = `${baseUrl || window.location.origin}/mcp`
  const claudeSnippet = `claude mcp add --transport http cam ${mcpUrl} --header "Authorization: Bearer ${sampleToken}"`
  const jsonSnippet = JSON.stringify(
    {
      mcpServers: {
        cam: {
          url: mcpUrl,
          headers: { Authorization: `Bearer ${sampleToken}` },
        },
      },
    },
    null,
    2,
  )
  const codexSnippet = `# ~/.codex/config.toml
[mcp_servers.cam]
url = "${mcpUrl}"
enabled = true
bearer_token_env_var = "CAM_TOKEN"  # export CAM_TOKEN=<your-token>
# alternatively, a static header:
# http_headers = { Authorization = "Bearer <your-token>" }`

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Agent setup</h1>
          <p className="text-sm text-muted-foreground">
            Generate CAM tokens and paste ready-made configs into your agents.
          </p>
        </div>
        <Button onClick={() => { setFormError(null); setCreateOpen(true) }}>
          Generate token
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>CAM tokens</CardTitle>
          <CardDescription>
            Each token identifies one agent or machine. Only a prefix is stored; the full
            token is shown once at creation.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {error && (
            <p className="text-sm text-destructive">
              {error instanceof ApiError ? error.message : "Could not load tokens."}
            </p>
          )}
          {data && activeTokens.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No active tokens. Generate one to connect an agent.
            </p>
          )}
          {activeTokens.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Label</TableHead>
                  <TableHead>Prefix</TableHead>
                  <TableHead>Last used</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {activeTokens.map((token) => (
                  <TableRow key={token.id}>
                    <TableCell className="font-medium">{token.label}</TableCell>
                    <TableCell className="font-mono text-xs">
                      cam_{token.token_prefix}…
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {token.last_used_at
                        ? new Date(token.last_used_at).toLocaleString()
                        : "never"}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {token.created_at
                        ? new Date(token.created_at).toLocaleString()
                        : "—"}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setRevokeTarget(token)}
                      >
                        Revoke
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {tokens.some((t) => t.revoked_at !== null) && (
            <div className="mt-4">
              <p className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
                Revoked
              </p>
              <div className="flex flex-wrap gap-2">
                {tokens
                  .filter((t) => t.revoked_at !== null)
                  .map((token) => (
                    <Badge key={token.id} variant="muted">
                      {token.label}
                    </Badge>
                  ))}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Connect an agent</CardTitle>
          <CardDescription>
            Configs use your CAM base URL. Replace the token placeholder with a real token
            if you have not created one yet.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <CopyRow label="Claude Code" value={claudeSnippet} />
          <CopyRow label="Cursor / generic MCP (JSON)" value={jsonSnippet} />
          <CopyRow label="Codex (config.toml)" value={codexSnippet} />
        </CardContent>
      </Card>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Generate token</DialogTitle>
            <DialogDescription>
              Give the token a label such as rahul-laptop so audit logs show which machine
              made a call.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="token-label">Label</Label>
            <Input
              id="token-label"
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="rahul-laptop"
            />
            {formError && <p className="text-xs text-destructive">{formError}</p>}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={createMutation.isPending || label.trim().length === 0}
              onClick={() => createMutation.mutate(label.trim())}
            >
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={rawToken !== null} onOpenChange={(open) => !open && setRawToken(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Token created</DialogTitle>
            <DialogDescription>
              Copy this token now. It is shown only once and cannot be recovered.
            </DialogDescription>
          </DialogHeader>
          {rawToken && <CopyRow label="CAM token" value={rawToken} />}
          <DialogFooter>
            <Button onClick={() => setRawToken(null)}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={revokeTarget !== null}
        onOpenChange={(open) => !open && setRevokeTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Revoke token?</DialogTitle>
            <DialogDescription>
              Agents using “{revokeTarget?.label}” will lose access immediately.
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
