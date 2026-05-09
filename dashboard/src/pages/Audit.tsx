import { useState, useCallback, useMemo, useEffect } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { AuditTable } from "@/components/audit/AuditTable"
import { AuditFilters } from "@/components/audit/AuditFilters"
import { useAudit } from "@/api/hooks/useAudit"
import { Button } from "@/components/ui/button"
import { ChevronLeft, ChevronRight } from "lucide-react"
import type { AuditEntry } from "@/api/types"

const PAGE_SIZES = [25, 50, 100]

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
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)

  useEffect(() => {
    setPage(1)
  }, [action, user, tailMode])

  const params = useMemo(() => {
    const p: Record<string, string> = {
      limit: String(pageSize),
      offset: String((page - 1) * pageSize),
    }
    if (action) p.action = action
    if (user) p.user_id = user
    return p
  }, [action, user, page, pageSize])

  const { data, isLoading, refetch } = useAudit(params, tailMode)

  const total = data?.total ?? 0
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const rangeStart = total === 0 ? 0 : (page - 1) * pageSize + 1
  const rangeEnd = Math.min(page * pageSize, total)

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

      <div className="mt-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs text-[var(--color-muted)]">Rows per page</span>
          <select
            value={pageSize}
            onChange={(e) => {
              setPageSize(Number(e.target.value))
              setPage(1)
            }}
            className="h-7 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-2 text-xs text-[var(--color-foreground)] focus:outline-none focus:border-[var(--color-accent)]"
          >
            {PAGE_SIZES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          {data != null && (
            <span className="text-xs text-[var(--color-muted)]">
              {total === 0 ? "0" : `${rangeStart}–${rangeEnd}`} of {total}
            </span>
          )}
        </div>

        {!tailMode && (
          <div className="flex items-center gap-1">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => p - 1)}
              disabled={page <= 1}
              className="h-7 w-7 p-0 border-[var(--color-border)]"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </Button>
            <span className="text-xs text-[var(--color-muted)] px-2 min-w-[4rem] text-center">
              {page} / {totalPages}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => p + 1)}
              disabled={page >= totalPages}
              className="h-7 w-7 p-0 border-[var(--color-border)]"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}
      </div>
    </PageContainer>
  )
}
