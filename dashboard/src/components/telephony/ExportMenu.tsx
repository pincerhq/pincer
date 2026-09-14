import { useState } from "react"
import { Download, Loader2 } from "lucide-react"
import { toast } from "sonner"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { downloadCsv, downloadFromApi, downloadJson } from "@/lib/download"
import { cn } from "@/lib/utils"

export interface LocalExport {
  label: string
  filename: string
  columns: string[]
  rows: Array<Record<string, unknown>>
}

/**
 * Download control.
 *
 * Server-backed datasets export the WHOLE current filter, not the page on
 * screen — an export that stops at the pagination boundary lands in a
 * spreadsheet looking complete. Chart series that already live in the browser
 * are written locally, because a round trip for forty points is silly.
 */
export function ExportMenu({
  dataset,
  query,
  local,
  label = "Export",
  compact = false,
}: {
  dataset?: "calls" | "turns" | "stages"
  query?: Record<string, string>
  local?: LocalExport
  label?: string
  compact?: boolean
}) {
  const [busy, setBusy] = useState(false)

  const serverDownload = async (format: "csv" | "json") => {
    if (!dataset) return
    setBusy(true)
    try {
      const params = new URLSearchParams({ ...(query ?? {}), dataset, format })
      const result = await downloadFromApi(`api/telephony/export?${params}`, `telephony-${dataset}.${format}`)
      toast.success(
        result.truncated
          ? `Exported ${result.rows.toLocaleString()} rows — capped, narrow the filter for the rest`
          : `Exported ${result.rows.toLocaleString()} rows`,
      )
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Export failed")
    } finally {
      setBusy(false)
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        disabled={busy}
        className={cn(
          "inline-flex items-center gap-1.5 rounded-md text-[11px] text-[var(--color-muted)]",
          "transition-colors hover:bg-white/[0.06] hover:text-[var(--color-foreground)]",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--color-accent)]",
          compact ? "h-6 w-6 justify-center" : "px-2 py-1",
        )}
      >
        {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Download className="h-3 w-3" />}
        {!compact && <span>{label}</span>}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[13rem]">
        {dataset && (
          <>
            <DropdownMenuLabel className="text-[10px] uppercase tracking-wide opacity-60">
              Current filter · all rows
            </DropdownMenuLabel>
            <DropdownMenuItem onSelect={() => void serverDownload("csv")}>{dataset} · CSV</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void serverDownload("json")}>{dataset} · JSON</DropdownMenuItem>
          </>
        )}
        {dataset && local && <DropdownMenuSeparator />}
        {local && (
          <>
            <DropdownMenuLabel className="text-[10px] uppercase tracking-wide opacity-60">
              {local.label}
            </DropdownMenuLabel>
            <DropdownMenuItem onSelect={() => downloadCsv(`${local.filename}.csv`, local.columns, local.rows)}>
              Chart data · CSV
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => downloadJson(`${local.filename}.json`, local.rows)}>
              Chart data · JSON
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
