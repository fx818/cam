import { FileText, LayoutGrid, LifeBuoy, ScrollText, Settings, Shield, Sparkles, Terminal, Users } from "lucide-react"
import { NavLink, Outlet } from "react-router-dom"

import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { useAuth } from "@/lib/auth"
import { cn } from "@/lib/utils"

const userNav = [
  { to: "/", label: "My services", icon: LayoutGrid, end: true },
  { to: "/agent-setup", label: "Agent setup", icon: Terminal },
  { to: "/activity", label: "Activity log", icon: ScrollText },
  { to: "/skills", label: "Skills", icon: Sparkles },
]

const adminNav = [
  { to: "/admin/users", label: "Users and teams", icon: Users },
  { to: "/admin/services", label: "Global services", icon: Shield },
  { to: "/admin/templates", label: "Templates", icon: FileText },
  { to: "/admin/overview", label: "Org overview", icon: LifeBuoy },
  { to: "/admin/settings", label: "Settings", icon: Settings },
]

function NavItem({
  to,
  label,
  icon: Icon,
  end,
}: {
  to: string
  label: string
  icon: typeof LayoutGrid
  end?: boolean
}) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
          isActive
            ? "bg-secondary text-secondary-foreground"
            : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
        )
      }
    >
      <Icon className="h-4 w-4" />
      {label}
    </NavLink>
  )
}

export function AppShell() {
  const { user, org, logout } = useAuth()
  const brand = (org?.settings?.brand as { name?: string } | undefined)?.name ?? org?.name ?? "CAM"

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r bg-card">
        <div className="flex h-14 items-center border-b px-4">
          <span className="text-lg font-semibold tracking-tight">{brand}</span>
        </div>
        <nav className="flex flex-1 flex-col gap-1 p-3">
          {userNav.map((item) => (
            <NavItem key={item.to} {...item} />
          ))}
          {user?.role === "admin" && (
            <>
              <Separator className="my-3" />
              <p className="px-3 py-1 text-xs font-semibold uppercase text-muted-foreground">
                Admin
              </p>
              {adminNav.map((item) => (
                <NavItem key={item.to} {...item} />
              ))}
            </>
          )}
        </nav>
        <div className="border-t p-3">
          <p className="truncate px-3 py-1 text-xs text-muted-foreground">{user?.email}</p>
          <Button variant="ghost" className="mt-1 w-full justify-start" onClick={() => void logout()}>
            Sign out
          </Button>
        </div>
      </aside>
      <main className="flex-1 overflow-x-hidden">
        <Outlet />
      </main>
    </div>
  )
}
