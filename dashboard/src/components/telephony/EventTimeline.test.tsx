import { render, screen } from "@testing-library/react"
import { describe, it, expect } from "vitest"
import { EventTimeline } from "./EventTimeline"
import type { TelephonyEvent } from "@/api/types"

function ev(name: string, ts: string, seq: number, turn_id = ""): TelephonyEvent {
  return {
    event_id: `${name}-${seq}`,
    call_id: "CA1",
    provider_call_id: "CA1",
    trace_id: "t",
    span_id: "s",
    turn_id,
    name,
    ts_utc: ts,
    seq,
    attributes: {},
  }
}

const rowsText = () =>
  (screen.getAllByRole("row") as HTMLElement[]).map((r) => r.textContent ?? "")

describe("event timeline ordering", () => {
  it("renders chronologically even when the list arrives out of order", () => {
    // The case the ordering exists for: a provider callback stamped early that
    // is appended after later events. Rendering in array order would put it at
    // the bottom AND make it the time origin's competitor.
    render(
      <EventTimeline
        events={[
          ev("call.answered", "2026-09-15T10:00:05.000Z", 2),
          ev("call.ended", "2026-09-15T10:00:30.000Z", 3),
          ev("call.registered", "2026-09-15T10:00:00.000Z", 1),
        ]}
      />,
    )

    const text = rowsText()
    expect(text[0]).toContain("call.registered")
    expect(text[1]).toContain("call.answered")
    expect(text[2]).toContain("call.ended")
  })

  it("takes the earliest event as the origin, so no offset is negative", () => {
    render(
      <EventTimeline
        events={[
          ev("call.answered", "2026-09-15T10:00:05.000Z", 2),
          ev("call.registered", "2026-09-15T10:00:00.000Z", 1),
        ]}
      />,
    )

    const text = rowsText()
    expect(text[0]).toContain("+0.00s")
    expect(text[1]).toContain("+5.00s")
    // "+-5.00s" is what the unsorted version rendered.
    expect(text.join(" ")).not.toContain("+-")
  })

  it("breaks a same-millisecond tie on seq, as the backend does", () => {
    // Both stamped in the same millisecond; `seq` is the only thing that says
    // which actually happened first, so a sort on the timestamp alone is not
    // enough to reproduce the backend's order.
    render(
      <EventTimeline
        events={[
          ev("second", "2026-09-15T10:00:00.000Z", 9),
          ev("first", "2026-09-15T10:00:00.000Z", 4),
        ]}
      />,
    )

    const text = rowsText()
    expect(text[0]).toContain("first")
    expect(text[1]).toContain("second")
  })

  it("keeps offsets relative to the call, not to the filtered view", () => {
    render(
      <EventTimeline
        turnId="turn-2"
        events={[
          ev("call.registered", "2026-09-15T10:00:00.000Z", 1),
          ev("turn.start", "2026-09-15T10:00:08.000Z", 2, "turn-2"),
        ]}
      />,
    )

    const text = rowsText()
    // call.registered has no turn_id so it survives the filter; turn.start is
    // 8s into the call and must still say so.
    expect(text.join(" ")).toContain("+8.00s")
  })
})
