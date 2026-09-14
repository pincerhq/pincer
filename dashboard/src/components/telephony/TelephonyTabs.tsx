import { NavLink, useLocation } from "react-router-dom"
import { ROUTES } from "@/lib/constants"
import { cn } from "@/lib/utils"

interface Tab {
  to: string
  label: string
  /** Rendered as a small badge; omitted when zero or unknown. */
  count?: number
  tone?: "danger" | "muted"
  end?: boolean
}

/**
 * Horizontal section nav for the Telephony area.
 *
 * Each section is its own route, so a link to "the latency view with these
 * filters" is a URL someone can paste into an incident channel — which is the
 * difference between a dashboard and a screenshot.
 */
export function TelephonyTabs({ firingAlerts, callCount }: { firingAlerts?: number; callCount?: number }) {
  const location = useLocation()

  const tabs: Tab[] = [
    { to: ROUTES.TELEPHONY, label: "Overview", end: true },
    { to: ROUTES.TELEPHONY_LATENCY, label: "Latency" },
    { to: ROUTES.TELEPHONY_RELIABILITY, label: "Reliability" },
    { to: ROUTES.TELEPHONY_ALERTS, label: "Alerts", count: firingAlerts, tone: "danger" },
    { to: ROUTES.TELEPHONY_CALLS, label: "Calls", count: callCount, tone: "muted" },
  ]

  return (
    <nav className="flex items-center gap-1 border-b border-[var(--color-border)]">
      {tabs.map((tab) => (
        <NavLink
          key={tab.to}
          to={{ pathname: tab.to, search: location.search }}
          end={tab.end}
          className={({ isActive }) =>
            cn(
              "relative -mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] transition-colors",
              isActive
                ? "border-[var(--color-accent)] text-[var(--color-foreground)]"
                : "border-transparent text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
            )
          }
        >
          {tab.label}
          {tab.count !== undefined && tab.count > 0 && (
            <span
              className={cn(
                "rounded-full px-1.5 py-0.5 text-[10px] leading-none tabular-nums",
                tab.tone === "danger" ? "bg-red-500/20 text-red-300" : "bg-white/[0.08] text-[var(--color-muted)]",
              )}
            >
              {tab.count}
            </span>
          )}
        </NavLink>
      ))}
    </nav>
  )
}
