import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { useState } from "react"

import { ConnectDialog, type ConnectTarget } from "@/components/ConnectDialog"
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
}: {
  service: DashboardService
  onConnect?: (service: DashboardService) => void
}) {
  const detailPath = `/services/${service.id}`
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
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {service.group === "templates" &&
          (service.connection ? (
            <Badge variant="success">connected</Badge>
          ) : (
            <Button size="sm" onClick={() => onConnect?.(service)}>
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
}: {
  title: string
  description: string
  services: DashboardService[]
  emptyText: string
  onConnect?: (service: DashboardService) => void
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
            />
          ))}
        </div>
      )}
    </section>
  )
}

export function MyServicesPage() {
  const queryClient = useQueryClient()
  const [connectTarget, setConnectTarget] = useState<ConnectTarget | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ["services"],
    queryFn: () => apiFetch<ServicesListResponse>("/api/services"),
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
            onConnect={(service) => setConnectTarget({ id: service.id, name: service.name })}
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

      {connectTarget && (
        <ConnectDialog
          target={connectTarget}
          open
          onOpenChange={(open) => {
            if (!open) {
              setConnectTarget(null)
              void queryClient.invalidateQueries({ queryKey: ["services"] })
            }
          }}
        />
      )}
    </div>
  )
}
