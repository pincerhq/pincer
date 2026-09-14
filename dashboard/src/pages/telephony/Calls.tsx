import { useState } from "react"
import { Skeleton } from "@/components/ui/skeleton"
import { CallTable } from "@/components/telephony/CallTable"
import { ExportMenu } from "@/components/telephony/ExportMenu"
import { Block } from "@/components/telephony/shared"
import { useTelephonyCalls } from "@/api/hooks/useTelephony"
import { useTelephonyContext } from "./context"

const PAGE_SIZE = 50

/** Every call in scope, searchable and sortable, each linking to its trace. */
export function TelephonyCalls() {
  const { filters, query } = useTelephonyContext()
  const [page, setPage] = useState({ limit: PAGE_SIZE, offset: 0, sort: "registered_at", order: "desc" })
  const calls = useTelephonyCalls(filters, page)

  const onSort = (column: string) =>
    setPage((prev) => ({
      ...prev,
      offset: 0,
      sort: column,
      order: prev.sort === column && prev.order === "desc" ? "asc" : "desc",
    }))

  return (
    <Block
      title="Calls"
      info={
        <>
          <p>
            Every call matching the filters — including calls that never connected and calls that
            were not sampled, so a failure is never missing from this list.
          </p>
          <p>
            Export downloads the whole filtered set, not just the page on screen. Phone numbers are
            masked at write time; no transcript, recording or tool payload exists in this data to
            export.
          </p>
        </>
      }
      actions={<ExportMenu dataset="calls" query={query} />}
    >
      {calls.isLoading || !calls.data ? (
        <Skeleton className="h-64 rounded-lg" />
      ) : (
        <CallTable
          calls={calls.data.calls}
          total={calls.data.total}
          offset={calls.data.offset}
          limit={calls.data.limit}
          sort={page.sort}
          order={page.order}
          onSort={onSort}
          onPage={(offset) => setPage((prev) => ({ ...prev, offset }))}
        />
      )}
    </Block>
  )
}
