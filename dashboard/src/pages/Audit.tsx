import { useState, useCallback, useMemo } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { AuditTable } from "@/components/audit/AuditTable"
import { AuditFilters } from "@/components/audit/AuditFilters"
import { useAudit } from "@/api/hooks/useAudit"
import type { AuditEntry } from "@/api/types"

function downloadFile(content: string, filename: string, mime: string) {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function entriesToCSV(entries: AuditEntry[]): string {
  const header = "timestamp,user_id,action,tool,approved,cost_usd,duration_ms\n"
  const rows = entries.map(
    (e) =>
      `${e.timestamp},${e.user_id},${e.action},${e.tool ?? ""},${e.approved},${e.cost_usd ?? ""},${e.duration_ms ?? ""}`,
  )
  return header + rows.join("\n")
}

export function AuditPage() {
  const [action, setAction] = useState("")
  const [user, setUser] = useState("")
  const [tailMode, setTailMode] = useState(false)

  const params = useMemo(() => {
    const p: Record<string, string> = { limit: "100" }
    if (action) p.action = action
    if (user) p.user = user
    return p
  }, [action, user])

  const { data, isLoading, refetch } = useAudit(params)

  const handleExportCSV = useCallback(() => {
    if (!data?.entries) return
    downloadFile(entriesToCSV(data.entries), "audit-log.csv", "text/csv")
  }, [data])

  const handleExportJSON = useCallback(() => {
    if (!data?.entries) return
    downloadFile(
      JSON.stringify(data.entries, null, 2),
      "audit-log.json",
      "application/json",
    )
  }, [data])

  return (
    <PageContainer title="Audit Log">
      <AuditFilters
        action={action}
        user={user}
        onActionChange={setAction}
        onUserChange={setUser}
        onExportCSV={handleExportCSV}
        onExportJSON={handleExportJSON}
        onRefresh={() => refetch()}
        tailMode={tailMode}
        onTailToggle={() => setTailMode((p) => !p)}
      />

      <div className="mt-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden">
        <AuditTable entries={data?.entries ?? []} loading={isLoading} />
      </div>

      {data?.total != null && (
        <p className="mt-3 text-xs text-[var(--color-muted)]">
          Showing {data.entries.length} of {data.total} entries
        </p>
      )}
    </PageContainer>
  )
}
