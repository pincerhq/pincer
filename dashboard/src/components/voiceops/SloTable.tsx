import type { SLOReport, SLOStatus } from "@/api/types"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"

function fmt(value: number | null, unit: string): string {
  if (value == null) return "—"
  return unit === "ratio" ? `${(value * 100).toFixed(1)}%` : `${value.toFixed(2)}${unit}`
}

function BurnBar({ slo }: { slo: SLOStatus }) {
  if (slo.burn_pct == null) return <span className="text-[var(--color-muted)]">—</span>
  const pct = Math.min(100, slo.burn_pct)
  const over = slo.burn_pct > 100
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-20 overflow-hidden rounded-full bg-[var(--color-border)]">
        <div
          className={cn("h-full", over ? "bg-red-500" : slo.burn_pct > 50 ? "bg-amber-500" : "bg-emerald-500")}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={cn("tabular-nums text-xs", over && "text-red-400")}>
        {slo.burn_pct.toFixed(0)}%
      </span>
    </div>
  )
}

export function SloTable({ report }: { report: SLOReport }) {
  return (
    <div className="space-y-3">
      {report.feature_freeze && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/5 p-4">
          <div className="text-sm font-medium text-red-300">🧊 Feature freeze in effect</div>
          <p className="mt-1 text-xs text-[var(--color-muted)]">{report.freeze_reason}</p>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-card)]">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>SLO</TableHead>
              <TableHead className="text-right">Actual</TableHead>
              <TableHead className="text-right">Target</TableHead>
              <TableHead>Error budget burned</TableHead>
              <TableHead className="text-right">Samples</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {report.slos.map((slo) => (
              <TableRow key={slo.name}>
                <TableCell className="font-medium">
                  {slo.name}
                  {slo.confidence === "inferred" && (
                    <span
                      className="ml-2 text-[10px] text-[var(--color-muted)]"
                      title="Only observed at canary run times — not a continuously measured number"
                    >
                      inferred
                    </span>
                  )}
                </TableCell>
                <TableCell
                  className={cn(
                    "text-right tabular-nums",
                    slo.met === false && "text-red-400",
                    slo.met === true && "text-emerald-400",
                  )}
                >
                  {fmt(slo.actual, slo.unit)}
                </TableCell>
                <TableCell className="text-right tabular-nums text-[var(--color-muted)]">
                  {fmt(slo.target, slo.unit)}
                </TableCell>
                <TableCell>
                  <BurnBar slo={slo} />
                </TableCell>
                <TableCell className="text-right tabular-nums text-[var(--color-muted)]">
                  {slo.sample_size}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <p className="text-[11px] text-[var(--color-muted)]">
        Freeze rule: more than {report.freeze_threshold_pct.toFixed(0)}% of an error budget burned
        mid-month stops feature work, once at least {report.freeze_min_sample} samples exist.
      </p>
    </div>
  )
}
