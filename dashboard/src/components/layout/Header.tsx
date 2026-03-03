import { useLocation } from "react-router-dom"
import { ROUTES } from "@/lib/constants"

const pageTitles: Record<string, string> = {
  [ROUTES.DASHBOARD]: "Dashboard",
  [ROUTES.COSTS]: "Costs",
  [ROUTES.CONVERSATIONS]: "Conversations",
  [ROUTES.AUDIT]: "Audit Log",
  [ROUTES.SKILLS]: "Skills",
  [ROUTES.DOCTOR]: "Security",
  [ROUTES.SETTINGS]: "Settings",
}

export function Header() {
  const location = useLocation()

  const matchedRoute = Object.keys(pageTitles).find((route) =>
    route === "/" ? location.pathname === "/" : location.pathname.startsWith(route),
  )
  const title = matchedRoute ? pageTitles[matchedRoute] : ""

  return (
    <header className="h-14 flex items-center border-b border-[var(--color-border)] px-6">
      <h1 className="text-sm font-medium text-[var(--color-foreground)]">
        {title}
      </h1>
    </header>
  )
}
