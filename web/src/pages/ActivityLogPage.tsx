import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ApiError, apiFetch } from "@/lib/api"
import type { AuditEntry, AuditListResponse } from "@/lib/types"

interface Filters {
  scope: string
  status: string
  agent: string
  service: string
  from: string
  to: string
}

const emptyFilters: Filters = {
  scope: "both",
  status: "",
  agent: "",
  service: "",
  from: "",
  to: "",
}

function buildParams(filters: Filters, cursor?: string): string {
  const params = new URLSearchParams()
  params.set("scope", filters.scope)
  if (filters.status) params.set("status", filters.status)
  if (filters.agent) params.set("agent", filters.agent)
  if (filters.service) params.set("service", filters.service)
  if (filters.from) params.set("from", filters.from)
  if (filters.to) params.set("to", filters.to)
  params.set("limit", "50")
  if (cursor) params.set("cursor", cursor)
  return params.toString()
}

export function ActivityLogPage() {
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [applied, setApplied] = useState<Filters>(emptyFilters)
  const [extraPages, setExtraPages] = useState<AuditEntry[][]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [selected, setSelected] = useState<AuditEntry | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ["audit", applied],
    queryFn: async () => {
      setExtraPages([])
      const response = await apiFetch<AuditListResponse>(
        `/api/audit?${buildParams(applied)}`,
      )
      setNextCursor(response.next_cursor)
      return response
    },
    placeholderData: keepPreviousData,
  })

  const loadMore = async () => {
    if (!nextCursor) return
    try {
      const response = await apiFetch<AuditListResponse>(
        `/api/audit?${buildParams(applied, nextCursor)}`,
      )
      setExtraPages((prev) => [...prev, response.entries])
      setNextCursor(response.next_cursor)
    } catch {
      setNextCursor(null)
    }
  }

  const openDetail = async (entry: AuditEntry) => {
    try {
      const response = await apiFetch<{ entry: AuditEntry }>(`/api/audit/${entry.id}`)
      setSelected(response.entry)
    } catch {
      setSelected(entry)
    }
  }

  const rows = [...(data?.entries ?? []), ...extraPages.flat()]

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Activity log</h1>
        <p className="text-sm text-muted-foreground">
          Your calls and calls made through services you own.
        </p>
      </div>

      <div className="grid gap-3 rounded-md border p-4 md:grid-cols-3 lg:grid-cols-6">
        <div className="space-y-1">
          <Label className="text-xs">Scope</Label>
          <Select
            value={filters.scope}
            onValueChange={(value) => setFilters({ ...filters, scope: value })}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="both">Both</SelectItem>
              <SelectItem value="mine">Mine</SelectItem>
              <SelectItem value="my_services">My services</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Status</Label>
          <Select
            value={filters.status || "any"}
            onValueChange={(value) =>
              setFilters({ ...filters, status: value === "any" ? "" : value })
            }
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="any">Any</SelectItem>
              <SelectItem value="ok">OK</SelectItem>
              <SelectItem value="error">Error</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Agent</Label>
          <Input
            value={filters.agent}
            onChange={(event) => setFilters({ ...filters, agent: event.target.value })}
            placeholder="claude-code"
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Service</Label>
          <Input
            value={filters.service}
            onChange={(event) => setFilters({ ...filters, service: event.target.value })}
            placeholder="global/redash"
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs">From</Label>
          <Input
            type="date"
            value={filters.from}
            onChange={(event) => setFilters({ ...filters, from: event.target.value })}
          />
        </div>
        <div className="space-y-1">
          <Label className="text-xs">To</Label>
          <Input
            type="date"
            value={filters.to}
            onChange={(event) => setFilters({ ...filters, to: event.target.value })}
          />
        </div>
        <div className="md:col-span-3 lg:col-span-6">
          <Button size="sm" onClick={() => { setNextCursor(null); setApplied(filters) }}>
            Apply filters
          </Button>
        </div>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load activity."}
        </p>
      )}

      {data && rows.length === 0 && (
        <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
          No activity matches these filters.
        </p>
      )}

      {rows.length > 0 && (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Time</TableHead>
                <TableHead>Actor</TableHead>
                <TableHead>Agent</TableHead>
                <TableHead>Action</TableHead>
                <TableHead>Service</TableHead>
                <TableHead>Tool</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((entry) => (
                <TableRow
                  key={entry.id}
                  className="cursor-pointer"
                  onClick={() => void openDetail(entry)}
                >
                  <TableCell className="whitespace-nowrap text-xs">
                    {entry.ts ? new Date(entry.ts).toLocaleString() : "—"}
                  </TableCell>
                  <TableCell className="text-xs">{entry.actor_email ?? "—"}</TableCell>
                  <TableCell className="text-xs">
                    {entry.agent_name ?? "—"}
                    {entry.token_label ? ` (${entry.token_label})` : ""}
                  </TableCell>
                  <TableCell className="text-xs">{entry.action}</TableCell>
                  <TableCell className="text-xs">{entry.service_ref ?? "—"}</TableCell>
                  <TableCell className="text-xs">{entry.tool_name ?? "—"}</TableCell>
                  <TableCell>
                    <Badge
                      variant={entry.status === "error" ? "destructive" : "success"}
                    >
                      {entry.status ?? "unknown"}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {nextCursor && (
        <Button variant="outline" onClick={() => void loadMore()}>
          Load more
        </Button>
      )}

      <Dialog open={selected !== null} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Activity detail</DialogTitle>
            <DialogDescription>
              {selected?.ts ? new Date(selected.ts).toLocaleString() : ""}
            </DialogDescription>
          </DialogHeader>
          {selected && (
            <div className="space-y-3 text-sm">
              <dl className="grid grid-cols-3 gap-2">
                <dt className="text-muted-foreground">Action</dt>
                <dd className="col-span-2">{selected.action}</dd>
                <dt className="text-muted-foreground">Actor</dt>
                <dd className="col-span-2">{selected.actor_email ?? "—"}</dd>
                <dt className="text-muted-foreground">Agent</dt>
                <dd className="col-span-2">
                  {selected.agent_name ?? "—"}
                  {selected.agent_version ? ` ${selected.agent_version}` : ""}
                </dd>
                <dt className="text-muted-foreground">Service</dt>
                <dd className="col-span-2">{selected.service_ref ?? "—"}</dd>
                <dt className="text-muted-foreground">Tool</dt>
                <dd className="col-span-2">{selected.tool_name ?? "—"}</dd>
                <dt className="text-muted-foreground">Latency</dt>
                <dd className="col-span-2">
                  {selected.latency_ms !== null ? `${selected.latency_ms} ms` : "—"}
                </dd>
                <dt className="text-muted-foreground">Status</dt>
                <dd className="col-span-2">
                  {selected.status ?? "—"}
                  {selected.error_code ? ` · ${selected.error_code}` : ""}
                </dd>
              </dl>
              {selected.error_message && (
                <div className="rounded-md bg-destructive/10 p-2 text-destructive">
                  {selected.error_message}
                </div>
              )}
              {selected.args !== undefined && (
                <div>
                  <p className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
                    Args
                  </p>
                  <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs">
                    {JSON.stringify(selected.args ?? null, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
