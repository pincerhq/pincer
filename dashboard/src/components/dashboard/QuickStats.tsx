import { MetricCard } from "./MetricCard"
import type { CostsToday, AuditStats } from "@/api/types"
import { formatCompactCurrency } from "@/lib/formatters"

interface QuickStatsProps {
  costs?: CostsToday
  auditStats?: AuditStats
  loading?: boolean
}

export function QuickStats({ costs, auditStats, loading }: QuickStatsProps) {
  return (
    <div className="grid grid-cols-4 gap-4">
      <MetricCard
        label="Today's Spend"
        value={formatCompactCurrency(costs?.total_usd ?? 0)}
        subtext={`${costs?.request_count ?? 0} requests`}
        trend={costs?.budget?.spent_pct}
        loading={loading}
      />
      <MetricCard
        label="Messages Today"
        value={auditStats?.by_action?.["message_received"] ?? 0}
        subtext="received"
        loading={loading}
      />
      <MetricCard
        label="Tool Calls"
        value={auditStats?.by_action?.["tool_call"] ?? 0}
        subtext="executed"
        loading={loading}
      />
      <MetricCard
        label="Errors"
        value={auditStats?.failed_actions ?? 0}
        subtext="failed actions"
        variant={
          (auditStats?.failed_actions ?? 0) > 0 ? "danger" : "default"
        }
        loading={loading}
      />
    </div>
  )
}
