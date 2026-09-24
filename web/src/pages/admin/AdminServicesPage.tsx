import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { Link } from "react-router-dom"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ApiError, apiFetch } from "@/lib/api"
import type { DashboardService, ServicesListResponse } from "@/lib/types"

function healthVariant(health: string): "success" | "destructive" | "warning" | "muted" {
  if (health === "healthy") return "success"
  if (health === "failing") return "destructive"
  if (health === "needs_reconnect") return "warning"
  return "muted"
}

function ServiceRows({
  services,
  onToggle,
  togglingId,
}: {
  services: DashboardService[]
  onToggle: (service: DashboardService) => void
  togglingId: string | null
}) {
  if (services.length === 0) {
    return (
      <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
        None yet. Global services are registered via POST /api/services (Phase 3).
      </p>
    )
  }
  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead>Ref</TableHead>
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
              <TableCell>
                <Badge variant={healthVariant(service.health)}>{service.health}</Badge>
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {service.status}
              </TableCell>
              <TableCell>{service.tool_count}</TableCell>
              <TableCell>
                <Switch
                  checked={service.status === "active"}
                  disabled={togglingId === service.id}
                  onCheckedChange={() => onToggle(service)}
                />
              </TableCell>
              <TableCell className="text-right">
                <Button size="sm" variant="outline" asChild>
                  <Link to={`/services/${service.id}`}>Open</Link>
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

export function AdminServicesPage() {
  const queryClient = useQueryClient()
  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ["services"],
    queryFn: () => apiFetch<ServicesListResponse>("/api/services"),
  })

  const toggleMutation = useMutation({
    mutationFn: (service: DashboardService) => {
      setTogglingId(service.id)
      return apiFetch<{ service: unknown }>(`/api/services/${service.id}`, {
        method: "PATCH",
        body: { status: service.status === "active" ? "disabled" : "active" },
      })
    },
    onSuccess: () => {
      setTogglingId(null)
      setMessage(null)
      void queryClient.invalidateQueries({ queryKey: ["services"] })
    },
    onError: (err) => {
      setTogglingId(null)
      setMessage(err instanceof ApiError ? err.message : "Could not update service.")
    },
  })

  const services = data?.services ?? []
  const globals = services.filter((s) => s.group === "global")

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Global services</h1>
        <p className="text-sm text-muted-foreground">
          Org-wide services everyone can reach. Creation and re-sync live with Phase 3/5
          routes.
        </p>
      </div>
      {message && <p className="text-sm text-destructive">{message}</p>}
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load services."}
        </p>
      )}
      {data && (
        <ServiceRows
          services={globals}
          onToggle={(service) => toggleMutation.mutate(service)}
          togglingId={togglingId}
        />
      )}
    </div>
  )
}

export function AdminTemplatesPage() {
  const queryClient = useQueryClient()
  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ["services"],
    queryFn: () => apiFetch<ServicesListResponse>("/api/services"),
  })

  const toggleMutation = useMutation({
    mutationFn: (service: DashboardService) => {
      setTogglingId(service.id)
      return apiFetch<{ service: unknown }>(`/api/services/${service.id}`, {
        method: "PATCH",
        body: { status: service.status === "active" ? "disabled" : "active" },
      })
    },
    onSuccess: () => {
      setTogglingId(null)
      setMessage(null)
      void queryClient.invalidateQueries({ queryKey: ["services"] })
    },
    onError: (err) => {
      setTogglingId(null)
      setMessage(err instanceof ApiError ? err.message : "Could not update template.")
    },
  })

  const services = data?.services ?? []
  const templates = services.filter((s) => s.group === "templates")

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Templates</h1>
        <p className="text-sm text-muted-foreground">
          Each user connects their own account; disabling a template hides it from My
          services.
        </p>
      </div>
      {message && <p className="text-sm text-destructive">{message}</p>}
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load templates."}
        </p>
      )}
      {data && (
        <ServiceRows
          services={templates}
          onToggle={(service) => toggleMutation.mutate(service)}
          togglingId={togglingId}
        />
      )}
    </div>
  )
}
