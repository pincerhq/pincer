import { useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { AlertList } from "@/components/voiceops/AlertList"
import { CallsTable } from "@/components/voiceops/CallsTable"
import { SignalCards } from "@/components/voiceops/SignalCards"
import { SloTable } from "@/components/voiceops/SloTable"
import {
  useCanaryRuns,
  useFailureBreakdown,
  useGoldenSignals,
  useOpsAlerts,
  useSlo,
  useTriggerCanary,
  useVoiceCalls,
} from "@/api/hooks/useVoiceOps"
import { formatDateTime } from "@/lib/formatters"
import { cn } from "@/lib/utils"

const WINDOWS = [
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
  { label: "30d", hours: 720 },
]

export function VoiceOpsPage() {
  const [windowHours, setWindowHours] = useState(168)

  const signals = useGoldenSignals()
  const alerts = useOpsAlerts()
  const slo = useSlo()
  const failures = useFailureBreakdown(windowHours)
  const canary = useCanaryRuns()
  const calls = useVoiceCalls(25)
  const triggerCanary = useTriggerCanary()

  return (
    <PageContainer title="Voice Ops">
      <div className="space-y-6">
        <section>
          <h2 className="mb-3 text-sm font-medium text-[var(--color-muted)]">Golden signals</h2>
          <SignalCards signals={signals.data?.signals} loading={signals.isLoading} />
        </section>

        <section>
          <h2 className="mb-3 text-sm font-medium text-[var(--color-muted)]">Alerts</h2>
          {alerts.isLoading ? (
            <Skeleton className="h-16 rounded-xl" />
          ) : (
            <AlertList alerts={alerts.data ?? []} />
          )}
        </section>

        <section>
          <h2 className="mb-3 text-sm font-medium text-[var(--color-muted)]">
            SLOs &amp; error budget
          </h2>
          {slo.isLoading || !slo.data ? (
            <Skeleton className="h-48 rounded-xl" />
          ) : (
            <SloTable report={slo.data} />
          )}
        </section>

        <section>
          <div className="mb-3 flex items-center gap-3">
            <h2 className="text-sm font-medium text-[var(--color-muted)]">Failure codes</h2>
            <div className="ml-auto flex gap-1">
              {WINDOWS.map((w) => (
                <Button
                  key={w.hours}
                  variant="outline"
                  size="sm"
                  onClick={() => setWindowHours(w.hours)}
                  className={cn(
                    "border-[var(--color-border)] text-xs",
                    windowHours === w.hours &&
                      "border-[var(--color-accent)]/30 bg-[var(--color-accent)]/10 text-[var(--color-accent)]",
                  )}
                >
                  {w.label}
                </Button>
              ))}
            </div>
          </div>
          {failures.isLoading ? (
            <Skeleton className="h-32 rounded-xl" />
          ) : failures.data && failures.data.codes.length > 0 ? (
            <div className="space-y-1.5 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-4">
              {failures.data.codes.map((entry) => (
                <div key={entry.code} className="flex items-center gap-3 text-xs">
                  <span className="w-40 shrink-0 font-mono">{entry.code}</span>
                  <span className="w-12 shrink-0 text-right tabular-nums">{entry.count}</span>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--color-border)]">
                    <div
                      className={cn(
                        "h-full",
                        entry.code === "none" ? "bg-emerald-500" : "bg-red-500/70",
                      )}
                      style={{
                        width: `${(entry.count / Math.max(1, failures.data.total)) * 100}%`,
                      }}
                    />
                  </div>
                  <span className="w-64 shrink-0 truncate text-[var(--color-muted)]">
                    {entry.description}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6 text-center text-sm text-[var(--color-muted)]">
              No terminated calls in this window.
            </div>
          )}
        </section>

        <section>
          <div className="mb-3 flex items-center gap-3">
            <h2 className="text-sm font-medium text-[var(--color-muted)]">Synthetic canary</h2>
            <Button
              variant="outline"
              size="sm"
              disabled={triggerCanary.isPending}
              onClick={() => triggerCanary.mutate()}
              className="ml-auto border-[var(--color-border)] text-xs"
              /* Says what it does: this is not a dry run. */
              title="Places a real phone call to the configured canary number"
            >
              {triggerCanary.isPending ? "Calling…" : "Run canary call now"}
            </Button>
          </div>
          {triggerCanary.isError && (
            <p className="mb-2 text-xs text-red-400">
              Canary could not run — check that PINCER_VOICE_CANARY_ENABLED and
              PINCER_VOICE_CANARY_NUMBER are set.
            </p>
          )}
          {canary.data && canary.data.length > 0 ? (
            <div className="space-y-1 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-4">
              {canary.data.map((run) => (
                <div key={run.ran_at} className="flex items-center gap-3 text-xs">
                  <span className="w-40 shrink-0 text-[var(--color-muted)]">
                    {formatDateTime(run.ran_at)}
                  </span>
                  <span
                    className={cn(
                      "w-20 shrink-0 font-medium",
                      run.skipped ? "text-amber-400" : run.ok ? "text-emerald-400" : "text-red-400",
                    )}
                  >
                    {run.skipped ? "skipped" : run.ok ? "ok" : "FAILED"}
                  </span>
                  <span className="w-16 shrink-0 tabular-nums text-[var(--color-muted)]">
                    {run.turns} turn{run.turns === 1 ? "" : "s"}
                  </span>
                  <span className="truncate text-[var(--color-muted)]">{run.reason}</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6 text-center text-sm text-[var(--color-muted)]">
              No canary runs recorded yet.
            </div>
          )}
        </section>

        <section>
          <h2 className="mb-3 text-sm font-medium text-[var(--color-muted)]">Recent calls</h2>
          <CallsTable calls={calls.data ?? []} loading={calls.isLoading} />
        </section>
      </div>
    </PageContainer>
  )
}
