import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from "recharts"
import type { ToolCost } from "@/api/types"
import { CHART_PALETTE, CHART_THEME } from "@/lib/constants"
import { Skeleton } from "@/components/ui/skeleton"

interface ToolBreakdownProps {
  tools: ToolCost[]
  loading?: boolean
}

function CustomTooltip({
  active,
  payload,
}: {
  active?: boolean
  payload?: Array<{ payload: ToolCost & { fill: string } }>
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
      <p className="text-[var(--color-foreground)] font-medium">{item.tool}</p>
      <p className="text-[var(--color-muted)] mt-1">
        ${item.total_usd.toFixed(4)} &middot; {item.call_count} calls
      </p>
    </div>
  )
}

export function ToolBreakdown({ tools, loading }: ToolBreakdownProps) {
  if (loading) {
    return <Skeleton className="w-full h-[200px] bg-white/[0.06]" />
  }

  if (!tools.length) {
    return (
      <div className="flex items-center justify-center h-[200px] text-sm text-[var(--color-muted)]">
        No tool data
      </div>
    )
  }

  const total = tools.reduce((s, t) => s + t.total_usd, 0)

  return (
    <div className="flex items-center gap-4">
      <ResponsiveContainer width="50%" height={200}>
        <PieChart>
          <Pie
            data={tools}
            dataKey="total_usd"
            nameKey="tool"
            cx="50%"
            cy="50%"
            innerRadius={50}
            outerRadius={75}
            strokeWidth={0}
          >
            {tools.map((_, i) => (
              <Cell
                key={i}
                fill={CHART_PALETTE[i % CHART_PALETTE.length]}
              />
            ))}
          </Pie>
          <Tooltip content={<CustomTooltip />} />
        </PieChart>
      </ResponsiveContainer>
      <div className="flex-1 space-y-2">
        {tools.map((t, i) => (
          <div key={t.tool} className="flex items-center gap-2 text-xs">
            <div
              className="h-2.5 w-2.5 rounded-sm shrink-0"
              style={{
                backgroundColor: CHART_PALETTE[i % CHART_PALETTE.length],
              }}
            />
            <span className="text-[var(--color-foreground)] truncate">
              {t.tool}
            </span>
            <span className="ml-auto font-mono text-[var(--color-muted)]">
              {total > 0 ? ((t.total_usd / total) * 100).toFixed(0) : 0}%
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
