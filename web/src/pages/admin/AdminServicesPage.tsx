import { AdminServiceRows, RegisterGlobalServiceForm, useAdminServices } from "./AdminServiceShared"

import { ApiError } from "@/lib/api"

function ServicesPageFrame({
  title,
  intro,
  tier,
}: {
  title: string
  intro: string
  tier: "global" | "template"
}) {
  const { data, isLoading, error } = useAdminServices()
  const services = (data?.services ?? []).filter((service) => service.tier === tier)

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">{title}</h1>
        <p className="text-sm text-muted-foreground">{intro}</p>
      </div>
      {error && (
        <p className="text-sm text-destructive">
          {error instanceof ApiError ? error.message : "Could not load services."}
        </p>
      )}
      <RegisterGlobalServiceForm />
      {isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {data && <AdminServiceRows services={services} />}
    </div>
  )
}

export function AdminServicesPage() {
  return (
    <ServicesPageFrame
      title="Global services"
      intro="Org-wide services everyone can reach with the org credential. Set visibility to everyone or to specific users per service."
      tier="global"
    />
  )
}

export function AdminTemplatesPage() {
  return (
    <ServicesPageFrame
      title="Templates"
      intro="Org-wide services each user connects to with their own account. Set who may connect by restricting visibility per service."
      tier="template"
    />
  )
}
