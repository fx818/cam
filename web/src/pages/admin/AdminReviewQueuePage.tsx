import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ApiError, apiFetch } from "@/lib/api"

interface ReviewQueueEntry {
  id: string
  name: string
  description: string | null
  service_id: string
  service_ref: string
  service_name: string
  tier: string
  status: string
  enabled: boolean
}

interface ReviewQueueResponse {
  tools: ReviewQueueEntry[]
}

interface ApproveResponse {
  approved: string[]
  unknown: string[]
}

export function AdminReviewQueuePage() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [message, setMessage] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin-review-queue"],
    queryFn: () => apiFetch<ReviewQueueResponse>("/api/admin/review-queue"),
  })

  const approveMutation = useMutation({
    mutationFn: (toolIds: string[]) =>
      apiFetch<ApproveResponse>("/api/admin/review-queue/approve", {
        method: "POST",
        body: { tool_ids: toolIds },
      }),
    onSuccess: (result) => {
      setSelected(new Set())
      setMessage(
        result.approved.length > 0
          ? `Approved ${result.approved.length} tool(s).`
          : null,
      )
      setFailure(null)
      void queryClient.invalidateQueries({ queryKey: ["admin-review-queue"] })
    },
    onError: (err) => {
      setFailure(err instanceof ApiError ? err.message : "Could not approve tools.")
    },
  })

  const tools = data?.tools ?? []

  function toggle(id: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  function approveSelection() {
    const ids = [...selected].filter((id) => tools.some((tool) => tool.id === id))
    if (ids.length > 0) {
      approveMutation.mutate(ids)
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Review queue</h1>
        <p className="text-sm text-muted-foreground">
          New tools found on global services and templates wait here until an admin
          approves them. Personal services auto-enable instead.
        </p>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load the review queue."}
        </p>
      )}
      {failure && <p className="text-sm text-destructive">{failure}</p>}
      {message && <p className="text-sm text-muted-foreground">{message}</p>}

      {data && tools.length === 0 && (
        <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
          Nothing pending. New tools appear here after a re-sync finds them.
        </p>
      )}

      {data && tools.length > 0 && (
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <Button
              disabled={selected.size === 0 || approveMutation.isPending}
              onClick={approveSelection}
            >
              Approve selected ({selected.size})
            </Button>
            <Button
              variant="outline"
              onClick={() => {
                setSelected(
                  selected.size === tools.length
                    ? new Set()
                    : new Set(tools.map((tool) => tool.id)),
                )
              }}
            >
              {selected.size === tools.length ? "Clear all" : "Select all"}
            </Button>
          </div>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>Tool</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead>Service</TableHead>
                  <TableHead>Tier</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {tools.map((tool) => (
                  <TableRow key={tool.id}>
                    <TableCell>
                      <input
                        type="checkbox"
                        aria-label={`Approve ${tool.name}`}
                        checked={selected.has(tool.id)}
                        onChange={() => toggle(tool.id)}
                      />
                    </TableCell>
                    <TableCell className="font-medium">{tool.name}</TableCell>
                    <TableCell className="max-w-md truncate text-xs text-muted-foreground">
                      {tool.description ?? "—"}
                    </TableCell>
                    <TableCell className="text-xs">{tool.service_ref}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{tool.tier}</Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}
    </div>
  )
}
