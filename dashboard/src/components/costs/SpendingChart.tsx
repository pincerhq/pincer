import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts"
import type { CostsHistoryEntry } from "@/api/types"
import { CHART_COLORS, CHART_THEME } from "@/lib/constants"
import { format, parseISO } from "date-fns"
import { Skeleton } from "@/components/ui/skeleton"

interface SpendingChartProps {
  data: CostsHistoryEntry[]
  height?: number
  loading?: boolean
}

function CustomTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: Array<{ value: number }>
  label?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div
      className="rounded-lg px-3 py-2 text-xs"
      style={{
        backgroundColor: CHART_THEME.tooltip.backgroundColor,
        border: CHART_THEME.tooltip.border,
      }}
    >
      <p className="text-[var(--color-muted)] mb-1">
        {label ? format(parseISO(label), "MMM d, yyyy") : ""}
      </p>
      <p className="text-[var(--color-foreground)] font-mono font-medium">
        ${payload[0].value.toFixed(4)}
      </p>
    </div>
  )
}

export function SpendingChart({
  data,
  height = 240,
  loading,
}: SpendingChartProps) {
  if (loading) {
    return <Skeleton className="w-full bg-white/[0.06]" style={{ height }} />
  }

  if (!data.length) {
    return (
      <div
        className="flex items-center justify-center text-sm text-[var(--color-muted)]"
        style={{ height }}
      >
        No spending data yet
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="spendGradient" x1="0" y1="0" x2="0" y2="1">
            <stop
              offset="0%"
              stopColor={CHART_COLORS.primary}
              stopOpacity={0.3}
            />
            <stop
              offset="100%"
              stopColor={CHART_COLORS.primary}
              stopOpacity={0}
            />
          </linearGradient>
        </defs>
        <CartesianGrid
          strokeDasharray="3 3"
          stroke={CHART_THEME.grid.stroke}
          vertical={false}
        />
        <XAxis
          dataKey="date"
          tick={{ fontSize: CHART_THEME.axis.fontSize, fill: CHART_THEME.axis.fill }}
          stroke={CHART_THEME.axis.stroke}
          tickFormatter={(v: string) => {
            try {
              return format(parseISO(v), "MMM d")
            } catch {
              return v
            }
          }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          tick={{ fontSize: CHART_THEME.axis.fontSize, fill: CHART_THEME.axis.fill }}
          stroke={CHART_THEME.axis.stroke}
          tickFormatter={(v: number) => `$${v.toFixed(2)}`}
          axisLine={false}
          tickLine={false}
          width={56}
        />
        <Tooltip content={<CustomTooltip />} />
        <Area
          type="monotone"
          dataKey="total_usd"
          stroke={CHART_COLORS.primary}
          strokeWidth={2}
          fill="url(#spendGradient)"
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
