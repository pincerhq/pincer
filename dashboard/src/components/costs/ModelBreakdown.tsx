import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts"
import type { ModelCost } from "@/api/types"
import { CHART_COLORS, CHART_THEME } from "@/lib/constants"
import { Skeleton } from "@/components/ui/skeleton"

interface ModelBreakdownProps {
  models: ModelCost[]
  loading?: boolean
}

function CustomTooltip({
  active,
  payload,
}: {
  active?: boolean
  payload?: Array<{ value: number; payload: ModelCost }>
}) {
  if (!active || !payload?.length) return null
  const item = payload[0].payload
  return (
    <div
      className="rounded-lg px-3 py-2 text-xs"
      style={{
        backgroundColor: CHART_THEME.tooltip.backgroundColor,
        border: CHART_THEME.tooltip.border,
      }}
    >
      <p className="text-[var(--color-foreground)] font-medium">{item.model}</p>
      <p className="text-[var(--color-muted)] mt-1">
        ${item.total_usd.toFixed(4)} &middot; {item.request_count} requests
      </p>
    </div>
  )
}

export function ModelBreakdown({ models, loading }: ModelBreakdownProps) {
  if (loading) {
    return <Skeleton className="w-full h-[200px] bg-white/[0.06]" />
  }

  if (!models.length) {
    return (
      <div className="flex items-center justify-center h-[200px] text-sm text-[var(--color-muted)]">
        No model data
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart
        data={models}
        layout="vertical"
        margin={{ top: 4, right: 4, left: 0, bottom: 0 }}
      >
        <CartesianGrid
          strokeDasharray="3 3"
          stroke={CHART_THEME.grid.stroke}
          horizontal={false}
        />
        <XAxis
          type="number"
          tick={{ fontSize: 11, fill: "#888" }}
          stroke={CHART_THEME.axis.stroke}
          tickFormatter={(v: number) => `$${v.toFixed(2)}`}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          type="category"
          dataKey="model"
          tick={{ fontSize: 11, fill: "#888" }}
          stroke={CHART_THEME.axis.stroke}
          axisLine={false}
          tickLine={false}
          width={100}
        />
        <Tooltip content={<CustomTooltip />} />
        <Bar
          dataKey="total_usd"
          fill={CHART_COLORS.secondary}
          radius={[0, 4, 4, 0]}
          barSize={20}
        />
      </BarChart>
    </ResponsiveContainer>
  )
}
