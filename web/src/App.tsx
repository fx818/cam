import { Navigate, Route, Routes } from "react-router-dom"
import type { ReactNode } from "react"

import { AppShell } from "@/components/AppShell"
import { useAuth } from "@/lib/auth"
import { ActivityLogPage } from "@/pages/ActivityLogPage"
import { AgentSetupPage } from "@/pages/AgentSetupPage"
import { LoginPage } from "@/pages/LoginPage"
import { MyServicesPage } from "@/pages/MyServicesPage"
import { RegisterServicePage } from "@/pages/RegisterServicePage"
import { ServiceDetailPage } from "@/pages/ServiceDetailPage"
import { SkillsPage } from "@/pages/SkillsPage"
import { AdminOverviewPage } from "@/pages/admin/AdminOverviewPage"
import { AdminSettingsPage } from "@/pages/admin/AdminSettingsPage"
import { AdminServicesPage, AdminTemplatesPage } from "@/pages/admin/AdminServicesPage"
import { AdminUsersPage } from "@/pages/admin/AdminUsersPage"

function LoadingScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
      Loading…
    </div>
  )
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  if (loading) {
    return <LoadingScreen />
  }
  if (!user) {
    return <Navigate to="/login" replace />
  }
  return <>{children}</>
}

function RequireAdmin({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  if (loading) {
    return <LoadingScreen />
  }
  if (!user) {
    return <Navigate to="/login" replace />
  }
  if (user.role !== "admin") {
    return <Navigate to="/" replace />
  }
  return <>{children}</>
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/" element={<MyServicesPage />} />
        <Route path="/services/new" element={<RegisterServicePage />} />
        <Route path="/services/:owner/:slug" element={<ServiceDetailPage />} />
        <Route path="/agent-setup" element={<AgentSetupPage />} />
        <Route path="/activity" element={<ActivityLogPage />} />
        <Route path="/skills" element={<SkillsPage />} />
        <Route
          path="/admin/users"
          element={
            <RequireAdmin>
              <AdminUsersPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/services"
          element={
            <RequireAdmin>
              <AdminServicesPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/templates"
          element={
            <RequireAdmin>
              <AdminTemplatesPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/overview"
          element={
            <RequireAdmin>
              <AdminOverviewPage />
            </RequireAdmin>
          }
        />
        <Route
          path="/admin/settings"
          element={
            <RequireAdmin>
              <AdminSettingsPage />
            </RequireAdmin>
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
