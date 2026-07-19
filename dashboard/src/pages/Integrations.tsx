import { useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { SkillGrid } from "@/components/skills/SkillGrid"
import { useIntegrations } from "@/api/hooks/useIntegrations"
import { Button } from "@/components/ui/button"
import { RefreshCw, LayoutGrid, List } from "lucide-react"
import { cn } from "@/lib/utils"
import type { IntegrationInfo } from "@/api/types"

type Filter = "all" | "tools" | "integrations" | "mcp"
type ViewMode = "grid" | "list"

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "tools", label: "Tools" },
  { id: "integrations", label: "Integrations" },
  { id: "mcp", label: "MCP" },
]

function sourceOf(s: IntegrationInfo): Exclude<Filter, "all"> {
  if (s.source === "builtin") return "tools"
  if (s.source === "mcp") return "mcp"
  return "integrations"
}

export function IntegrationsPage() {
  const [filter, setFilter] = useState<Filter>("all")
  const [view, setView] = useState<ViewMode>("grid")

  const { data, isLoading, refetch } = useIntegrations()

  const all = data?.integrations ?? []
  const visible = filter === "all" ? all : all.filter((s) => sourceOf(s) === filter)

  const counts = {
    all: all.length,
    tools: all.filter((s) => sourceOf(s) === "tools").length,
    integrations: all.filter((s) => sourceOf(s) === "integrations").length,
    mcp: all.filter((s) => sourceOf(s) === "mcp").length,
  }

  return (
    <PageContainer title="Integrations">
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-5">
        {/* Filter tabs */}
        <div className="flex items-center gap-1 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={cn(
                "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
                filter === f.id
                  ? "bg-white/[0.08] text-[var(--color-foreground)]"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              {f.label}
              {counts[f.id] > 0 && (
                <span
                  className={cn(
                    "text-[10px] font-mono rounded px-1 py-px",
                    filter === f.id
                      ? "bg-white/[0.1] text-[var(--color-foreground)]"
                      : "bg-white/[0.04] text-[var(--color-muted)]",
                  )}
                >
                  {counts[f.id]}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Right controls */}
        <div className="flex items-center gap-2">
          {/* View toggle */}
          <div className="flex items-center rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-1">
            <button
              onClick={() => setView("grid")}
              title="Grid view"
              className={cn(
                "p-1.5 rounded-md transition-colors",
                view === "grid"
                  ? "bg-white/[0.08] text-[var(--color-foreground)]"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              <LayoutGrid className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setView("list")}
              title="List view"
              className={cn(
                "p-1.5 rounded-md transition-colors",
                view === "list"
                  ? "bg-white/[0.08] text-[var(--color-foreground)]"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              <List className="h-3.5 w-3.5" />
            </button>
          </div>

          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            className="border-[var(--color-border)]"
          >
            <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
            Refresh
          </Button>
        </div>
      </div>

      {/* Content */}
      <SkillGrid skills={visible} loading={isLoading} viewMode={view} />
      {!isLoading && all.length === 0 && <EmptyState />}
    </PageContainer>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-[var(--color-muted)]">
      <p className="text-sm">Nothing here yet</p>
      <p className="text-xs mt-1">Connect Google/Slack or configure an MCP server to extend your agent</p>
    </div>
  )
}
