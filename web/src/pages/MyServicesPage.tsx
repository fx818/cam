import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ApiError, apiFetch } from "@/lib/api"
import type { DashboardService, ServicesListResponse } from "@/lib/types"

function healthVariant(health: string): "success" | "destructive" | "warning" | "muted" {
  if (health === "healthy") return "success"
  if (health === "failing") return "destructive"
  if (health === "needs_reconnect") return "warning"
  return "muted"
}

function ServiceRow({
  service,
  onConnect,
  connectPending,
  connectError,
}: {
  service: DashboardService
  onConnect?: (service: DashboardService) => void
  connectPending?: boolean
  connectError?: string | null
}) {
  const detailPath =
    service.tier === "template" ? null : `/services/${service.id}`
  return (
    <div className="flex items-center justify-between gap-4 rounded-md border p-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          {detailPath ? (
            <Link
              to={detailPath}
              className="truncate font-medium hover:underline"
            >
              {service.name}
            </Link>
          ) : (
            <span className="truncate font-medium">{service.name}</span>
          )}
          <Badge variant={healthVariant(service.health)}>{service.health}</Badge>
          <Badge variant="outline">{service.tier}</Badge>
        </div>
        <p className="mt-0.5 truncate text-xs text-muted-foreground">
          {service.id}
          {service.shared_by ? ` · shared by ${service.shared_by}` : ""}
          {` · ${service.tool_count} tools`}
        </p>
        {service.description && (
          <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
            {service.description}
          </p>
        )}
        {connectError && <p className="mt-1 text-xs text-destructive">{connectError}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {service.group === "templates" &&
          (service.connection ? (
            <Badge variant="success">connected</Badge>
          ) : (
            <Button
              size="sm"
              disabled={connectPending}
              onClick={() => onConnect?.(service)}
            >
              Connect
            </Button>
          ))}
        {detailPath && (
          <Button size="sm" variant="outline" asChild>
            <Link to={detailPath}>Open</Link>
          </Button>
        )}
      </div>
    </div>
  )
}

function Section({
  title,
  description,
  services,
  emptyText,
  onConnect,
  connectPending,
  connectErrors,
}: {
  title: string
  description: string
  services: DashboardService[]
  emptyText: string
  onConnect?: (service: DashboardService) => void
  connectPending?: boolean
  connectErrors?: Record<string, string | null>
}) {
  return (
    <section>
      <div className="mb-2">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          {title}
        </h2>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      {services.length === 0 ? (
        <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
          {emptyText}
        </p>
      ) : (
        <div className="space-y-2">
          {services.map((service) => (
            <ServiceRow
              key={`${service.group}:${service.id}`}
              service={service}
              onConnect={onConnect}
              connectPending={connectPending}
              connectError={connectErrors?.[service.id] ?? null}
            />
          ))}
        </div>
      )}
    </section>
  )
}

export function MyServicesPage() {
  const queryClient = useQueryClient()
  const [connectErrors, setConnectErrors] = useState<Record<string, string | null>>({})

  const { data, isLoading, error } = useQuery({
    queryKey: ["services"],
    queryFn: () => apiFetch<ServicesListResponse>("/api/services"),
  })

  const connectMutation = useMutation({
    mutationFn: (service: DashboardService) =>
      apiFetch<unknown>(`/api/services/${service.id}/connect`, {
        method: "POST",
        body: {},
      }),
    onSuccess: () => {
      setConnectErrors((prev) => ({ ...prev }))
      void queryClient.invalidateQueries({ queryKey: ["services"] })
    },
    onError: (err, service) => {
      const message =
        err instanceof ApiError ? err.message : "Could not connect. Try again."
      setConnectErrors((prev) => ({ ...prev, [service.id]: message }))
    },
  })

  const services = data?.services ?? []
  const byGroup = (group: string) => services.filter((s) => s.group === group)

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">My services</h1>
          <p className="text-sm text-muted-foreground">
            Everything you can reach through CAM.
          </p>
        </div>
        <Button asChild>
          <Link to="/services/new">Register service</Link>
        </Button>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load services."}
        </p>
      )}

      {data && (
        <div className="space-y-6">
          <Section
            title="Global"
            description="Services registered by admins for everyone."
            services={byGroup("global")}
            emptyText="No global services yet."
          />
          <Section
            title="Templates"
            description="Connect your own account once; share it like any service."
            services={byGroup("templates")}
            emptyText="No templates available yet."
            onConnect={(service) => connectMutation.mutate(service)}
            connectPending={connectMutation.isPending}
            connectErrors={connectErrors}
          />
          <Section
            title="My services"
            description="Personal services you registered."
            services={byGroup("mine")}
            emptyText="You have not registered a personal service yet."
          />
          <Section
            title="Shared with me"
            description="Services other users shared with you."
            services={byGroup("shared")}
            emptyText="Nothing has been shared with you yet."
          />
        </div>
      )}
    </div>
  )
}
