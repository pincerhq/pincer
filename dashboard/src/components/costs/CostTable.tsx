import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { ModelCost } from "@/api/types"
import { formatCompactCurrency, formatTokens } from "@/lib/formatters"
import { Skeleton } from "@/components/ui/skeleton"

interface CostTableProps {
  models: ModelCost[]
  loading?: boolean
}

export function CostTable({ models, loading }: CostTableProps) {
  if (loading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full bg-white/[0.06]" />
        ))}
      </div>
    )
  }

  if (!models.length) {
    return (
      <p className="text-sm text-[var(--color-muted)] py-8 text-center">
        No cost data available
      </p>
    )
  }

  const sorted = [...models].sort((a, b) => b.total_usd - a.total_usd)

  return (
    <Table>
      <TableHeader>
        <TableRow className="border-[var(--color-border)] hover:bg-transparent">
          <TableHead className="text-[var(--color-muted)]">Model</TableHead>
          <TableHead className="text-[var(--color-muted)] text-right">
            Requests
          </TableHead>
          <TableHead className="text-[var(--color-muted)] text-right">
            Tokens
          </TableHead>
          <TableHead className="text-[var(--color-muted)] text-right">
            Cost
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.map((m) => (
          <TableRow
            key={m.model}
            className="border-[var(--color-border)] hover:bg-white/[0.02]"
          >
            <TableCell className="font-mono text-sm">{m.model}</TableCell>
            <TableCell className="text-right text-[var(--color-muted)]">
              {m.request_count}
            </TableCell>
            <TableCell className="text-right text-[var(--color-muted)] font-mono">
              {formatTokens(m.total_tokens)}
            </TableCell>
            <TableCell className="text-right font-mono font-medium">
              {formatCompactCurrency(m.total_usd)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
