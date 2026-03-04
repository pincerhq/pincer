import { PageContainer } from "@/components/layout/PageContainer"
import { QuickStats } from "@/components/dashboard/QuickStats"
import { ChannelStatus } from "@/components/dashboard/ChannelStatus"
import { BudgetGauge } from "@/components/dashboard/BudgetGauge"
import { ActivityFeed } from "@/components/dashboard/ActivityFeed"
import { SpendingChart } from "@/components/costs/SpendingChart"
import { useStatus } from "@/api/hooks/useStatus"
import { useCostsToday, useCostsHistory } from "@/api/hooks/useCosts"
import { useAuditStats } from "@/api/hooks/useAudit"

export function DashboardPage() {
  const { data: status, isLoading: statusLoading } = useStatus()
  const { data: costs, isLoading: costsLoading } = useCostsToday()
  const { data: history, isLoading: historyLoading } = useCostsHistory(7)
  const { data: auditStats, isLoading: auditLoading } = useAuditStats()

  const loading = costsLoading || auditLoading

  return (
    <PageContainer title="Dashboard">
      <QuickStats costs={costs} auditStats={auditStats} loading={loading} />

      <div className="grid grid-cols-3 gap-4 mt-6">
        <div className="col-span-2">
          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6">
            <h3 className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider mb-4">
              Spending — Last 7 Days
            </h3>
            <SpendingChart
              data={history?.data ?? []}
              height={240}
              loading={historyLoading}
            />
          </div>
        </div>

        <div className="space-y-4">
          <BudgetGauge
            spent={costs?.budget?.spent_pct ?? 0}
            limit={costs?.budget?.daily_limit ?? 5}
            remaining={costs?.budget?.remaining ?? 5}
            loading={costsLoading}
          />
          <ChannelStatus
            channels={status?.channels}
            loading={statusLoading}
          />
        </div>
      </div>

      <div className="mt-6">
        <ActivityFeed />
      </div>
    </PageContainer>
  )
}
