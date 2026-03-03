import type { DoctorCheck } from "@/api/types"
import { cn } from "@/lib/utils"

interface CheckItemProps {
  check: DoctorCheck
}

const STATUS_DOT: Record<string, string> = {
  pass: "bg-[var(--color-success)]",
  warn: "bg-[var(--color-warning)]",
  fail: "bg-[var(--color-danger)]",
}

export function CheckItem({ check }: CheckItemProps) {
  return (
    <div className="flex items-start gap-3 py-2.5 px-3 rounded-lg hover:bg-white/[0.02] transition-colors">
      <div
        className={cn(
          "mt-1 h-2.5 w-2.5 rounded-full shrink-0",
          STATUS_DOT[check.status] ?? "bg-[var(--color-muted)]",
        )}
      />
      <div className="flex-1 min-w-0">
        <p className="text-sm text-[var(--color-foreground)]">{check.name}</p>
        <p className="text-xs text-[var(--color-muted)] mt-0.5">
          {check.message}
        </p>
        {check.fix_hint && check.status !== "pass" && (
          <p className="text-xs text-[var(--color-accent)] mt-1 font-mono">
            Fix: {check.fix_hint}
          </p>
        )}
      </div>
    </div>
  )
}
