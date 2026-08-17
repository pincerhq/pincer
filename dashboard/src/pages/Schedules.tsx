import { useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { SchedulesTable } from "@/components/schedules/SchedulesTable"
import { useSchedules } from "@/api/hooks/useSchedules"
import { Button } from "@/components/ui/button"

export function SchedulesPage() {
  const [includePast, setIncludePast] = useState(false)
  const { data, isLoading, isError, refetch } = useSchedules(includePast)

  return (
    <PageContainer title="Schedules">
      <div className="flex items-center gap-3 flex-wrap">
        <div className="ml-auto flex items-center gap-2">
          {data != null && (
            <span className="text-xs text-[var(--color-muted)]">
              {data.future_count} recurring / upcoming
              {includePast ? ` · ${data.past_count} past` : ""}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => setIncludePast((p) => !p)}
            className={`border-[var(--color-border)] text-xs ${includePast ? "bg-[var(--color-accent)]/10 text-[var(--color-accent)] border-[var(--color-accent)]/30" : ""}`}
          >
            {includePast ? "Show past: ON" : "Show past: OFF"}
          </Button>
        </div>
      </div>

      <div className="mt-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden">
        <SchedulesTable
          tasks={data?.tasks ?? []}
          loading={isLoading}
          error={isError}
          onRetry={() => void refetch()}
        />
      </div>
    </PageContainer>
  )
}
