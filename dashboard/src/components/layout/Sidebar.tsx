import { Link, useLocation, useNavigate } from "react-router-dom"
import {
  LayoutDashboard,
  DollarSign,
  MessageSquare,
  Shield,
  Puzzle,
  Stethoscope,
  Settings,
  PanelLeftClose,
  PanelLeft,
  LogOut,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { ROUTES } from "@/lib/constants"
import { usePreferencesStore } from "@/stores/preferences"
import { useAuthStore } from "@/stores/auth"
import { useStatus } from "@/api/hooks/useStatus"

const navigation = [
  { name: "Dashboard", href: ROUTES.DASHBOARD, icon: LayoutDashboard },
  { name: "Costs", href: ROUTES.COSTS, icon: DollarSign },
  { name: "Conversations", href: ROUTES.CONVERSATIONS, icon: MessageSquare },
  { name: "Audit Log", href: ROUTES.AUDIT, icon: Shield },
  { name: "Skills", href: ROUTES.SKILLS, icon: Puzzle },
  { name: "Security", href: ROUTES.DOCTOR, icon: Stethoscope },
  { name: "Settings", href: ROUTES.SETTINGS, icon: Settings },
]

export function Sidebar() {
  const location = useLocation()
  const navigate = useNavigate()
  const { sidebarCollapsed, toggleSidebar } = usePreferencesStore()
  const logout = useAuthStore((s) => s.logout)
  const { data: status } = useStatus()

  const isRunning = status?.agent_running ?? false

  const handleLogout = () => {
    logout()
    navigate(ROUTES.LOGIN)
  }

  return (
    <aside
      className={cn(
        "fixed left-0 top-0 z-40 h-screen border-r border-[var(--color-border)] bg-[var(--color-background)] flex flex-col transition-[width] duration-200",
        sidebarCollapsed
          ? "w-[var(--sidebar-collapsed-width)]"
          : "w-[var(--sidebar-width)]",
      )}
    >
      <div className="flex h-14 items-center border-b border-[var(--color-border)] px-4">
        {!sidebarCollapsed && (
          <span className="text-base font-semibold tracking-tight">
            pincer
          </span>
        )}
        <button
          onClick={toggleSidebar}
          className={cn(
            "flex items-center justify-center rounded-md p-1.5 text-[var(--color-muted)] hover:text-[var(--color-foreground)] hover:bg-white/[0.04] transition-colors",
            sidebarCollapsed ? "mx-auto" : "ml-auto",
          )}
        >
          {sidebarCollapsed ? (
            <PanelLeft className="h-4 w-4" />
          ) : (
            <PanelLeftClose className="h-4 w-4" />
          )}
        </button>
      </div>

      <nav className="flex-1 px-2 py-3 space-y-0.5">
        {navigation.map((item) => {
          const isActive =
            item.href === "/"
              ? location.pathname === "/"
              : location.pathname.startsWith(item.href)
          return (
            <Link
              key={item.href}
              to={item.href}
              title={sidebarCollapsed ? item.name : undefined}
              className={cn(
                "flex items-center gap-3 rounded-lg text-sm transition-colors relative",
                sidebarCollapsed ? "justify-center px-2 py-2.5" : "px-3 py-2.5",
                isActive
                  ? "text-[var(--color-foreground)] bg-white/[0.06]"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)] hover:bg-white/[0.03]",
              )}
            >
              {isActive && (
                <div className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 bg-[var(--color-accent)] rounded-r-full" />
              )}
              <item.icon className="h-4 w-4 shrink-0" />
              {!sidebarCollapsed && <span>{item.name}</span>}
            </Link>
          )
        })}
      </nav>

      <div className="px-4 py-3 border-t border-[var(--color-border)] space-y-2">
        <div className="flex items-center gap-2">
          <div
            className={cn(
              "h-2 w-2 rounded-full",
              isRunning
                ? "bg-[var(--color-success)] animate-pulse"
                : "bg-[var(--color-danger)]",
            )}
          />
          {!sidebarCollapsed && (
            <span className="text-xs text-[var(--color-muted)]">
              {isRunning ? "Agent running" : "Disconnected"}
            </span>
          )}
        </div>
        <button
          onClick={handleLogout}
          title={sidebarCollapsed ? "Log out" : undefined}
          className={cn(
            "flex items-center gap-3 w-full rounded-lg text-sm text-[var(--color-muted)] hover:text-[var(--color-foreground)] hover:bg-white/[0.03] transition-colors",
            sidebarCollapsed ? "justify-center px-2 py-2" : "px-3 py-2",
          )}
        >
          <LogOut className="h-4 w-4 shrink-0" />
          {!sidebarCollapsed && <span>Log out</span>}
        </button>
      </div>
    </aside>
  )
}
