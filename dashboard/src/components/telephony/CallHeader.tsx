import { AlertTriangle } from "lucide-react"
import type { TelephonyCall } from "@/api/types"
import { Ms } from "./shared"

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-[var(--color-muted)]">{label}</div>
      <div className="mt-0.5 text-xs">{value || "—"}</div>
    </div>
  )
}

/** Identity, configuration and outcome of one call — everything an engineer
 *  needs before looking at a single timing. Phone numbers are masked at write
 *  time; this page never has the full number to show. */
export function CallHeader({ call, gaps }: { call: TelephonyCall; gaps: string[] }) {
  return (
    <div className="space-y-3">
      <div className="grid gap-3 rounded-xl border border-[var(--color-border)] bg-white/[0.02] p-4 sm:grid-cols-3 lg:grid-cols-5">
        <Field label="Provider call id" value={<span className="font-mono">{call.provider_call_id}</span>} />
        <Field label="Internal call id" value={<span className="font-mono">{call.call_id}</span>} />
        <Field label="Trace id" value={<span className="font-mono">{call.trace_id}</span>} />
        <Field label="Direction" value={call.direction} />
        <Field label="Status" value={`${call.status}${call.outcome ? ` (${call.outcome})` : ""}`} />

        <Field label="Engine" value={call.engine} />
        <Field label="Transport" value={call.transport} />
        <Field label="Codec / rate" value={`${call.codec || "—"}${call.sample_rate ? ` @ ${call.sample_rate} Hz` : ""}`} />
        <Field label="Model" value={call.model} />
        <Field label="Language" value={call.language} />

        <Field label="From" value={<span className="font-mono">{call.from_number_masked}</span>} />
        <Field label="To" value={<span className="font-mono">{call.to_number_masked}</span>} />
        <Field label="Environment" value={call.environment} />
        <Field label="Version" value={call.app_version} />
        <Field label="Tenant" value={call.tenant_id} />

        <Field label="Registered" value={call.registered_at ? new Date(call.registered_at).toLocaleString() : "—"} />
        <Field label="Answered" value={call.answered_at ? new Date(call.answered_at).toLocaleString() : "never"} />
        <Field label="Ended" value={call.ended_at ? new Date(call.ended_at).toLocaleString() : "—"} />
        <Field label="Setup" value={<Ms value={call.setup_ms} />} />
        <Field label="Duration" value={<Ms value={call.duration_ms} />} />

        <Field label="Turns" value={String(call.turn_count)} />
        <Field label="Tools" value={String(call.tool_count)} />
        <Field
          label="Errors / timeouts"
          value={`${call.error_count} / ${call.timeout_count}`}
        />
        <Field
          label="Interruptions / reconnects"
          value={`${call.interruption_count} / ${call.reconnect_count}`}
        />
        <Field
          label="Termination"
          value={
            <span>
              {call.failure_code || "none"}
              {call.termination_reason && (
                <span className="text-[var(--color-muted)]"> · {call.termination_reason}</span>
              )}
            </span>
          }
        />
      </div>

      {gaps.length > 0 && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/[0.06] px-4 py-3 text-xs text-amber-200">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <p className="font-medium">Telemetry is incomplete for this call</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {gaps.map((gap) => (
                  <li key={gap}>{gap}</li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
