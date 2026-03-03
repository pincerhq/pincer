import { useCallback } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { ScoreRing } from "@/components/doctor/ScoreRing"
import { DoctorReport } from "@/components/doctor/DoctorReport"
import { useDoctor } from "@/api/hooks/useSettings"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { RefreshCw, Download, Loader2 } from "lucide-react"

export function DoctorPage() {
  const { data, isLoading, isFetching, refetch } = useDoctor()

  const handleExport = useCallback(() => {
    if (!data) return
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = "security-report.json"
    a.click()
    URL.revokeObjectURL(url)
  }, [data])

  return (
    <PageContainer title="Security">
      <div className="flex items-center justify-between mb-6">
        <p className="text-sm text-[var(--color-muted)]">
          Security health check for your Pincer agent
        </p>
        <div className="flex items-center gap-2">
          {data && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleExport}
              className="border-[var(--color-border)]"
            >
              <Download className="h-3.5 w-3.5 mr-1.5" />
              Export
            </Button>
          )}
          <Button
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
            className="bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90"
          >
            {isFetching ? (
              <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
            )}
            Run Check
          </Button>
        </div>
      </div>

      {!data && !isLoading ? (
        <div className="flex flex-col items-center justify-center py-20">
          <p className="text-sm text-[var(--color-muted)]">
            Click &quot;Run Check&quot; to scan your agent&apos;s security
          </p>
        </div>
      ) : isLoading ? (
        <div className="flex flex-col items-center gap-6 py-12">
          <Skeleton className="h-40 w-40 rounded-full bg-white/[0.06]" />
          <Skeleton className="h-4 w-48 bg-white/[0.06]" />
        </div>
      ) : data ? (
        <div className="space-y-8">
          <div className="flex flex-col items-center">
            <ScoreRing score={data.score} />
            <div className="flex items-center gap-6 mt-4 text-xs">
              <span className="text-[var(--color-success)]">
                {data.passed} passed
              </span>
              <span className="text-[var(--color-warning)]">
                {data.warnings} warnings
              </span>
              <span className="text-[var(--color-danger)]">
                {data.critical} critical
              </span>
            </div>
          </div>

          <DoctorReport checks={data.checks} />
        </div>
      ) : null}
    </PageContainer>
  )
}
