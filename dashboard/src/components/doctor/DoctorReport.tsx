import { useState } from "react"
import type { DoctorCheck } from "@/api/types"
import { CheckItem } from "./CheckItem"
import { ChevronDown, ChevronRight } from "lucide-react"
import { cn } from "@/lib/utils"

interface DoctorReportProps {
  checks: DoctorCheck[]
}

const CATEGORY_ORDER = [
  "secrets",
  "access",
  "budget",
  "filesystem",
  "network",
  "dependencies",
  "runtime",
]

function groupByCategory(checks: DoctorCheck[]) {
  const groups: Record<string, DoctorCheck[]> = {}
  for (const check of checks) {
    const cat = check.category.toLowerCase()
    if (!groups[cat]) groups[cat] = []
    groups[cat].push(check)
  }
  return groups
}

function categoryLabel(cat: string): string {
  return cat.charAt(0).toUpperCase() + cat.slice(1)
}

function categorySummary(checks: DoctorCheck[]): { pass: number; warn: number; fail: number } {
  return checks.reduce(
    (acc, c) => {
      acc[c.status]++
      return acc
    },
    { pass: 0, warn: 0, fail: 0 },
  )
}

export function DoctorReport({ checks }: DoctorReportProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const groups = groupByCategory(checks)

  const sortedCategories = [
    ...CATEGORY_ORDER.filter((c) => groups[c]),
    ...Object.keys(groups).filter((c) => !CATEGORY_ORDER.includes(c)),
  ]

  const toggle = (cat: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(cat)) next.delete(cat)
      else next.add(cat)
      return next
    })
  }

  return (
    <div className="space-y-2">
      {sortedCategories.map((cat) => {
        const items = groups[cat]
        const isOpen = !collapsed.has(cat)
        const summary = categorySummary(items)

        return (
          <div
            key={cat}
            className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden"
          >
            <button
              onClick={() => toggle(cat)}
              className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-white/[0.02] transition-colors"
            >
              {isOpen ? (
                <ChevronDown className="h-4 w-4 text-[var(--color-muted)]" />
              ) : (
                <ChevronRight className="h-4 w-4 text-[var(--color-muted)]" />
              )}
              <span className="text-sm font-medium">
                {categoryLabel(cat)}
              </span>
              <div className="ml-auto flex items-center gap-2">
                {summary.fail > 0 && (
                  <span className="text-[10px] font-mono text-[var(--color-danger)]">
                    {summary.fail} critical
                  </span>
                )}
                {summary.warn > 0 && (
                  <span className="text-[10px] font-mono text-[var(--color-warning)]">
                    {summary.warn} warnings
                  </span>
                )}
                <span
                  className={cn(
                    "text-[10px] font-mono",
                    summary.fail > 0
                      ? "text-[var(--color-danger)]"
                      : summary.warn > 0
                        ? "text-[var(--color-warning)]"
                        : "text-[var(--color-success)]",
                  )}
                >
                  {summary.pass}/{items.length}
                </span>
              </div>
            </button>
            {isOpen && (
              <div className="px-4 pb-3 space-y-0.5">
                {items.map((check) => (
                  <CheckItem key={check.id} check={check} />
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
