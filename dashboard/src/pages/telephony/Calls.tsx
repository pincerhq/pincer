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

  // Narrowing the filters can strand the offset past the end of the new result
  // set: page to 100, filter down to 12 matches, and the table renders empty
  // with the range "101–12 of 12" and a `Previous` that is still enabled (the
  // only way back). `onSort` already resets the offset for the same reason;
  // changing the filters needs it just as much.
  //
  // Adjusted during render rather than from an effect, and keyed on a stable
  // string rather than the filters object. An effect would let one render —
  // and one fetch — go out with the new filters and the stale offset before
  // correcting itself; a render-phase reset is discarded before it commits, so
  // that request is never made. `filters` is a fresh object every time the URL
  // changes, whereas `filterParams` emits its keys in a fixed order, so the
  // serialisation is a dependable identity for "the filters changed".
  const signature = JSON.stringify(query)
  const [lastSignature, setLastSignature] = useState(signature)
  if (signature !== lastSignature) {
    setLastSignature(signature)
    setPage((prev) => ({ ...prev, offset: 0 }))
  }

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
