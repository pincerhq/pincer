import { Outlet } from "react-router-dom"
import { Sidebar } from "./Sidebar"
import { Header } from "./Header"
import { usePreferencesStore } from "@/stores/preferences"
import { cn } from "@/lib/utils"

export function AppLayout() {
  const sidebarCollapsed = usePreferencesStore((s) => s.sidebarCollapsed)

  return (
    <div className="min-h-screen bg-[var(--color-background)]">
      <Sidebar />
      <div
        className={cn(
          "transition-[margin-left] duration-200",
          sidebarCollapsed
            ? "ml-[var(--sidebar-collapsed-width)]"
            : "ml-[var(--sidebar-width)]",
        )}
      >
        <Header />
        <main className="min-h-[calc(100vh-3.5rem)]">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
