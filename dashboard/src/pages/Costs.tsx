import { useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { MetricCard } from "@/components/dashboard/MetricCard"
import { SpendingChart } from "@/components/costs/SpendingChart"
import { ModelBreakdown } from "@/components/costs/ModelBreakdown"
import { ToolBreakdown } from "@/components/costs/ToolBreakdown"
import { CostTable } from "@/components/costs/CostTable"
import { BudgetAlert } from "@/components/costs/BudgetAlert"
import {
  useCostsToday,
  useCostsHistory,
  useCostsByModel,
  useCostsByTool,
} from "@/api/hooks/useCosts"
import { formatCompactCurrency } from "@/lib/formatters"
import { cn } from "@/lib/utils"

type Period = 7 | 30

export function CostsPage() {
  const [period, setPeriod] = useState<Period>(7)
  const { data: today, isLoading: todayLoading } = useCostsToday()
  const { data: history, isLoading: historyLoading } = useCostsHistory(period)
  const { data: byModel, isLoading: modelLoading } = useCostsByModel(period)
  const { data: byTool, isLoading: toolLoading } = useCostsByTool(period)

  return (
    <PageContainer title="Costs">
      <BudgetAlert budget={today?.budget} />

      <div className="flex items-center justify-between mt-2 mb-6">
        <div />
        <div className="flex items-center gap-1 rounded-lg bg-white/[0.04] p-1">
          {([7, 30] as const).map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={cn(
                "px-3 py-1 rounded-md text-xs font-medium transition-colors",
                period === p
                  ? "bg-white/[0.08] text-[var(--color-foreground)]"
                  : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
              )}
            >
              {p}d
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4">
        <MetricCard
          label="Today"
          value={formatCompactCurrency(today?.total_usd ?? 0)}
          subtext={`${today?.request_count ?? 0} requests`}
          loading={todayLoading}
        />
        <MetricCard
          label={`Last ${period} Days`}
          value={formatCompactCurrency(history?.totals?.total_usd ?? 0)}
          subtext={`${history?.totals?.total_requests ?? 0} requests`}
          loading={historyLoading}
        />
        <MetricCard
          label="Budget Status"
          value={`${(today?.budget?.spent_pct ?? 0).toFixed(0)}%`}
          subtext={`${formatCompactCurrency(today?.budget?.remaining ?? 0)} remaining`}
          trend={today?.budget?.spent_pct}
          loading={todayLoading}
        />
      </div>

      <div className="mt-6 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6">
        <h3 className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider mb-4">
          Daily Spending — Last {period} Days
        </h3>
        <SpendingChart
          data={history?.data ?? []}
          height={280}
          loading={historyLoading}
        />
      </div>

      <div className="grid grid-cols-2 gap-4 mt-6">
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6">
          <h3 className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider mb-4">
            By Model
          </h3>
          <ModelBreakdown
            models={byModel?.models ?? []}
            loading={modelLoading}
          />
        </div>
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6">
          <h3 className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider mb-4">
            By Tool
          </h3>
          <ToolBreakdown tools={byTool?.tools ?? []} loading={toolLoading} />
        </div>
      </div>

      <div className="mt-6 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6">
        <h3 className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider mb-4">
          Model Details
        </h3>
        <CostTable models={byModel?.models ?? []} loading={modelLoading} />
      </div>
    </PageContainer>
  )
}
