import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"

import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ApiError, apiFetch } from "@/lib/api"
import type { OverviewResponse } from "@/lib/types"

function StatCard({
  title,
  value,
  description,
}: {
  title: string
  value: string | number
  description?: string
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-3xl font-semibold">{value}</p>
        {description && (
          <p className="mt-1 text-xs text-muted-foreground">{description}</p>
        )}
      </CardContent>
    </Card>
  )
}

export function AdminOverviewPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["admin-overview"],
    queryFn: () => apiFetch<OverviewResponse>("/api/admin/overview"),
  })

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Org overview</h1>
        <p className="text-sm text-muted-foreground">
          Usage metrics arrive later (Phase 8.5); this is health and recent activity.
        </p>
      </div>

      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load overview."}
        </p>
      )}

      {data && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard
              title="Users"
              value={data.users.active}
              description={`${data.users.total} total · ${data.users.deactivated} deactivated`}
            />
            <StatCard
              title="Admins"
              value={data.users.admins}
              description="Active admin accounts"
            />
            <StatCard
              title="Services"
              value={
                (data.services.by_tier.global ?? 0) +
                (data.services.by_tier.template ?? 0) +
                (data.services.by_tier.personal ?? 0)
              }
              description={`${data.services.by_tier.global ?? 0} global · ${
                data.services.by_tier.template ?? 0
              } templates · ${data.services.by_tier.personal ?? 0} personal`}
            />
            <StatCard
              title="Failing services"
              value={data.services.by_health.failing ?? 0}
              description={`${data.services.by_health.healthy ?? 0} healthy · ${
                data.services.by_health.unknown ?? 0
              } unknown`}
            />
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Recent activity</CardTitle>
              <CardDescription>Latest calls across the org.</CardDescription>
            </CardHeader>
            <CardContent>
              {data.recent_audit.length === 0 ? (
                <p className="text-sm text-muted-foreground">No activity yet.</p>
              ) : (
                <div className="rounded-md border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Time</TableHead>
                        <TableHead>Actor</TableHead>
                        <TableHead>Action</TableHead>
                        <TableHead>Service</TableHead>
                        <TableHead>Status</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.recent_audit.map((entry) => (
                        <TableRow key={entry.id}>
                          <TableCell className="whitespace-nowrap text-xs">
                            {entry.ts ? new Date(entry.ts).toLocaleString() : "—"}
                          </TableCell>
                          <TableCell className="text-xs">
                            {entry.actor_email ?? "—"}
                          </TableCell>
                          <TableCell className="text-xs">{entry.action}</TableCell>
                          <TableCell className="text-xs">
                            {entry.service_ref ? (
                              <Link
                                to={`/services/${entry.service_ref}`}
                                className="hover:underline"
                              >
                                {entry.service_ref}
                              </Link>
                            ) : (
                              "—"
                            )}
                          </TableCell>
                          <TableCell>
                            <Badge
                              variant={
                                entry.status === "error" ? "destructive" : "success"
                              }
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
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
