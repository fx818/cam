import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
import { Textarea } from "@/components/ui/textarea"
import { ApiError, apiFetch } from "@/lib/api"
import { useIdentityKey } from "@/lib/auth"
import type {
  AdminSettings,
  AdminSettingsResponse,
  AllowlistEntry,
  AllowlistResponse,
} from "@/lib/types"

export function AdminSettingsPage() {
  const queryClient = useQueryClient()
  const identity = useIdentityKey()
  const [timeoutMs, setTimeoutMs] = useState<string | null>(null)
  const [cap, setCap] = useState<string | null>(null)
  const [retention, setRetention] = useState<string | null>(null)
  const [domains, setDomains] = useState<string | null>(null)
  const [instructions, setInstructions] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const [pattern, setPattern] = useState("")
  const [note, setNote] = useState("")
  const [allowMessage, setAllowMessage] = useState<string | null>(null)

  const settingsQuery = useQuery({
    queryKey: ["admin-settings", identity],
    queryFn: () => apiFetch<AdminSettingsResponse>("/api/admin/settings"),
  })

  const allowQuery = useQuery({
    queryKey: ["admin-allowlist", identity],
    queryFn: () => apiFetch<AllowlistResponse>("/api/admin/url-allowlist"),
  })

  const settings = settingsQuery.data?.settings

  const patchMutation = useMutation({
    mutationFn: (body: Partial<AdminSettings>) =>
      apiFetch<AdminSettingsResponse>("/api/admin/settings", {
        method: "PATCH",
        body,
      }),
    onSuccess: () => {
      setMessage("Saved.")
      setTimeoutMs(null)
      setCap(null)
      setRetention(null)
      setDomains(null)
      setInstructions(null)
      void queryClient.invalidateQueries({ queryKey: ["admin-settings"] })
    },
    onError: (err) => {
      setMessage(err instanceof ApiError ? err.message : "Could not save settings.")
    },
  })

  const addAllowMutation = useMutation({
    mutationFn: () =>
      apiFetch<{ entry: AllowlistEntry }>("/api/admin/url-allowlist", {
        method: "POST",
        body: { pattern: pattern.trim(), note: note.trim() || null },
      }),
    onSuccess: () => {
      setPattern("")
      setNote("")
      setAllowMessage(null)
      void queryClient.invalidateQueries({ queryKey: ["admin-allowlist"] })
    },
    onError: (err) => {
      setAllowMessage(
        err instanceof ApiError ? err.message : "Could not add pattern.",
      )
    },
  })

  const removeAllowMutation = useMutation({
    mutationFn: (entryId: string) =>
      apiFetch<{ ok: boolean }>(`/api/admin/url-allowlist/${entryId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["admin-allowlist"] })
    },
    onError: (err) => {
      setAllowMessage(
        err instanceof ApiError ? err.message : "Could not remove pattern.",
      )
    },
  })

  const saveSettings = () => {
    const body: Partial<AdminSettings> = {}
    if (timeoutMs !== null) body.default_timeout_ms = Number(timeoutMs)
    if (cap !== null) body.response_cap_tokens = Number(cap)
    if (retention !== null) body.log_retention_days = Number(retention)
    if (domains !== null)
      body.allowed_email_domains = domains
        .split(",")
        .map((d) => d.trim())
        .filter(Boolean)
    if (instructions !== null) body.instructions = instructions
    patchMutation.mutate(body)
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Org defaults, sign-up domains and the outbound URL allowlist.
        </p>
      </div>

      {settingsQuery.isLoading && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}
      {settingsQuery.error && (
        <p className="text-sm text-destructive">
          {settingsQuery.error instanceof ApiError
            ? settingsQuery.error.message
            : "Could not load settings."}
        </p>
      )}

      {settings && (
        <Card>
          <CardHeader>
            <CardTitle>Org defaults</CardTitle>
            <CardDescription>
              Applied to new services; existing services keep their own limits.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid max-w-xl gap-4">
            {message && <p className="text-sm text-muted-foreground">{message}</p>}
            <div className="grid grid-cols-3 gap-3">
              <div className="space-y-1">
                <Label htmlFor="st-timeout">Timeout (ms)</Label>
                <Input
                  id="st-timeout"
                  inputMode="numeric"
                  value={timeoutMs ?? String(settings.default_timeout_ms)}
                  onChange={(e) => setTimeoutMs(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="st-cap">Response cap</Label>
                <Input
                  id="st-cap"
                  inputMode="numeric"
                  value={cap ?? String(settings.response_cap_tokens)}
                  onChange={(e) => setCap(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="st-retention">Log retention (days)</Label>
                <Input
                  id="st-retention"
                  inputMode="numeric"
                  value={retention ?? String(settings.log_retention_days)}
                  onChange={(e) => setRetention(e.target.value)}
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="st-domains">Allowed email domains (comma-separated)</Label>
              <Input
                id="st-domains"
                value={
                  domains ?? settings.allowed_email_domains.join(", ")
                }
                onChange={(e) => setDomains(e.target.value)}
                placeholder="indiamart.com"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="st-instructions">Org instructions for agents</Label>
              <Textarea
                id="st-instructions"
                rows={4}
                value={instructions ?? settings.instructions}
                onChange={(e) => setInstructions(e.target.value)}
              />
            </div>
            <div>
              <Button
                disabled={patchMutation.isPending}
                onClick={saveSettings}
              >
                Save settings
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>URL allowlist</CardTitle>
          <CardDescription>
            Hostnames or CIDRs exempt from the private-range block for personal services.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Alert>
            Patterns are exact hostnames (intranet.example.com) or CIDRs (10.0.0.0/8).
            Global and template services are never restricted.
          </Alert>
          {allowMessage && (
            <p className="text-sm text-destructive">{allowMessage}</p>
          )}
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-52 space-y-1">
              <Label htmlFor="al-pattern">Pattern</Label>
              <Input
                id="al-pattern"
                value={pattern}
                onChange={(e) => setPattern(e.target.value)}
                placeholder="10.0.0.0/8"
              />
            </div>
            <div className="min-w-52 space-y-1">
              <Label htmlFor="al-note">Note</Label>
              <Input
                id="al-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="staging network"
              />
            </div>
            <Button
              disabled={addAllowMutation.isPending || pattern.trim().length === 0}
              onClick={() => addAllowMutation.mutate()}
            >
              Add
            </Button>
          </div>

          {allowQuery.isLoading && (
            <p className="text-sm text-muted-foreground">Loading…</p>
          )}
          {allowQuery.data && allowQuery.data.entries.length === 0 && (
            <p className="text-sm text-muted-foreground">No allowlist entries.</p>
          )}
          {allowQuery.data && allowQuery.data.entries.length > 0 && (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Pattern</TableHead>
                    <TableHead>Note</TableHead>
                    <TableHead>Added by</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {allowQuery.data.entries.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell>
                        <Badge variant="outline">{entry.pattern}</Badge>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground">
                        {entry.note ?? "—"}
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">
                        {entry.created_by_email ?? "—"}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={removeAllowMutation.isPending}
                          onClick={() => removeAllowMutation.mutate(entry.id)}
                        >
                          Remove
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
