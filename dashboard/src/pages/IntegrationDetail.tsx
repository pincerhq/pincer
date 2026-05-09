import { useState } from "react"
import { useParams, useNavigate } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { PageContainer } from "@/components/layout/PageContainer"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import { ArrowLeft, ChevronDown, ChevronRight, Wrench, Activity } from "lucide-react"
import { ROUTES } from "@/lib/constants"

const INTEGRATION_ICONS: Record<string, string> = {
  google: "G",
  ms365: "M",
  slack: "#",
}

const INTEGRATION_COLORS: Record<string, string> = {
  google: "bg-blue-500/10 text-blue-400 border-blue-500/20",
  ms365: "bg-cyan-500/10 text-cyan-400 border-cyan-500/20",
  slack: "bg-violet-500/10 text-violet-400 border-violet-500/20",
}

export function IntegrationDetailPage() {
  const { slug = "" } = useParams<{ slug: string }>()
  const navigate = useNavigate()
  const [openCategories, setOpenCategories] = useState<Set<string>>(new Set())

  const { data, isLoading, error } = useQuery({
    queryKey: ["integration", slug],
    queryFn: () => pincer.integration(slug),
    enabled: !!slug,
    staleTime: 60_000,
  })

  const toggleCategory = (name: string) => {
    setOpenCategories((prev) => {
      const next = new Set(prev)
      next.has(name) ? next.delete(name) : next.add(name)
      return next
    })
  }

  const topTools = data
    ? Object.entries(data.usage.by_tool)
        .sort(([, a], [, b]) => b - a)
        .slice(0, 10)
    : []

  return (
    <PageContainer title="">
      {/* Back link */}
      <button
        onClick={() => navigate(ROUTES.SKILLS)}
        className="flex items-center gap-1.5 text-sm text-[var(--color-muted)] hover:text-[var(--color-foreground)] mb-6 transition-colors"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Skills &amp; Integrations
      </button>

      {isLoading && (
        <div className="space-y-4">
          <Skeleton className="h-24 w-full bg-white/[0.06] rounded-xl" />
          <Skeleton className="h-40 w-full bg-white/[0.06] rounded-xl" />
          <Skeleton className="h-64 w-full bg-white/[0.06] rounded-xl" />
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-8 text-center text-[var(--color-muted)]">
          <p className="text-sm">Failed to load integration details.</p>
        </div>
      )}

      {data && (
        <div className="space-y-6">
          {/* Header card */}
          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6">
            <div className="flex items-start gap-4">
              <div
                className={cn(
                  "h-12 w-12 rounded-xl border flex items-center justify-center text-lg font-bold shrink-0",
                  INTEGRATION_COLORS[slug] ?? "bg-white/[0.06] text-[var(--color-muted)] border-[var(--color-border)]",
                )}
              >
                {INTEGRATION_ICONS[slug] ?? "?"}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-3 flex-wrap">
                  <h1 className="text-lg font-semibold">{data.name}</h1>
                  <span
                    className={cn(
                      "text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-full",
                      data.status === "active"
                        ? "bg-[var(--color-success)]/15 text-[var(--color-success)]"
                        : "bg-white/[0.06] text-[var(--color-muted)]",
                    )}
                  >
                    {data.status}
                  </span>
                </div>
                <p className="text-sm text-[var(--color-muted)] mt-1">{data.description}</p>
                <div className="flex items-center gap-4 mt-3 text-xs text-[var(--color-muted)]">
                  <span className="flex items-center gap-1.5">
                    <Wrench className="h-3.5 w-3.5" />
                    {data.tool_count} tools
                  </span>
                  <span className="flex items-center gap-1.5">
                    <Activity className="h-3.5 w-3.5" />
                    {data.usage.total_calls} total calls
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* Usage stats */}
          {topTools.length > 0 && (
            <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-muted)] mb-4">
                Top Tools by Usage
              </h2>
              <div className="space-y-2">
                {topTools.map(([tool, count]) => {
                  const pct = Math.round((count / data.usage.total_calls) * 100)
                  return (
                    <div key={tool} className="flex items-center gap-3">
                      <span className="text-xs font-mono text-[var(--color-muted)] w-48 truncate shrink-0">
                        {tool}
                      </span>
                      <div className="flex-1 h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
                        <div
                          className="h-full rounded-full bg-[var(--color-accent)]"
                          style={{ width: `${pct}%` }}
                        />
                      </div>
                      <span className="text-xs text-[var(--color-muted)] w-10 text-right shrink-0">
                        {count}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {data.status === "disabled" && (
            <div className="rounded-xl border border-[var(--color-border)] bg-amber-500/5 border-amber-500/20 p-4 text-sm text-amber-400/80">
              This integration is not configured. Credentials or tokens are missing — run{" "}
              <code className="font-mono text-xs bg-white/[0.06] px-1.5 py-0.5 rounded">
                pincer setup-{slug === "ms365" ? "ms365" : slug === "google" ? "google" : "slack"}
              </code>{" "}
              to set it up.
            </div>
          )}

          {/* Tool categories */}
          <div className="space-y-2">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-muted)] mb-3">
              Tools ({data.tool_count})
            </h2>
            {data.categories.map((cat) => {
              const isOpen = openCategories.has(cat.name)
              return (
                <div
                  key={cat.name}
                  className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden"
                >
                  <button
                    onClick={() => toggleCategory(cat.name)}
                    className="w-full flex items-center justify-between px-4 py-3 hover:bg-white/[0.03] transition-colors"
                  >
                    <div className="flex items-center gap-3">
                      {isOpen ? (
                        <ChevronDown className="h-3.5 w-3.5 text-[var(--color-muted)]" />
                      ) : (
                        <ChevronRight className="h-3.5 w-3.5 text-[var(--color-muted)]" />
                      )}
                      <span className="text-sm font-medium">{cat.name}</span>
                    </div>
                    <span className="text-xs text-[var(--color-muted)]">
                      {cat.tools.length} tool{cat.tools.length !== 1 ? "s" : ""}
                    </span>
                  </button>
                  {isOpen && (
                    <div className="border-t border-[var(--color-border)] divide-y divide-[var(--color-border)]">
                      {cat.tools.map((tool) => {
                        const calls = data.usage.by_tool[tool.name] ?? 0
                        return (
                          <div key={tool.name} className="px-4 py-3 flex items-start gap-3">
                            <div className="flex-1 min-w-0">
                              <p className="text-xs font-mono text-[var(--color-foreground)]">
                                {tool.name}
                              </p>
                              <p className="text-xs text-[var(--color-muted)] mt-0.5 leading-relaxed">
                                {tool.description}
                              </p>
                            </div>
                            {calls > 0 && (
                              <span className="text-[10px] font-mono text-[var(--color-muted)] shrink-0 mt-0.5">
                                {calls}×
                              </span>
                            )}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </PageContainer>
  )
}
