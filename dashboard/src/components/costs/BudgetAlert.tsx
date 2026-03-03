import { AlertTriangle } from "lucide-react"
import type { BudgetInfo } from "@/api/types"
import { formatCompactCurrency } from "@/lib/formatters"

interface BudgetAlertProps {
  budget?: BudgetInfo
}

export function BudgetAlert({ budget }: BudgetAlertProps) {
  if (!budget || budget.spent_pct < 80) return null

  const isCritical = budget.spent_pct >= 100

  return (
    <div
      className={`flex items-center gap-3 rounded-lg px-4 py-3 text-sm ${
        isCritical
          ? "bg-red-500/10 border border-red-500/20 text-red-400"
          : "bg-yellow-500/10 border border-yellow-500/20 text-yellow-400"
      }`}
    >
      <AlertTriangle className="h-4 w-4 shrink-0" />
      <span>
        {isCritical
          ? `Daily budget exceeded! Spent ${formatCompactCurrency(budget.spent_today)} of ${formatCompactCurrency(budget.daily_limit)}.`
          : `${budget.spent_pct.toFixed(0)}% of daily budget used. ${formatCompactCurrency(budget.remaining)} remaining.`}
      </span>
    </div>
  )
}
