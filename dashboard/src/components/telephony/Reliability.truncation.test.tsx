import { render, screen } from "@testing-library/react"
import { describe, it, expect } from "vitest"
import { MemoryRouter } from "react-router-dom"
import { FailureCodes } from "./FailureCodes"
import { OutcomeMix } from "./OutcomeMix"

/** `n` calls, alternating outcome so both components have rows to draw. */
function calls(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    call_id: `CA${i}`,
    provider_call_id: `CA${i}`,
    failure_category: i % 2 ? "technical" : "none",
    failure_code: i % 2 ? "ws_drop" : "none",
    registered_at: "2026-09-15T10:00:00Z",
    ended_at: "2026-09-15T10:01:00Z",
  }))
}

function renderBoth(loaded: number, total: number) {
  const props = {
    calls: calls(loaded) as never,
    total,
    filters: { hours: 24 } as never,
    onChange: () => {},
    query: {},
  }
  return render(
    <MemoryRouter>
      <OutcomeMix {...props} />
      <FailureCodes {...props} />
    </MemoryRouter>,
  )
}

describe("truncated aggregates say so", () => {
  it("says nothing when the whole window fits under the cap", () => {
    renderBoth(120, 120)
    expect(screen.queryByText(/most recent of/)).toBeNull()
  })

  it("names the sample and the real total on both blocks when the list is capped", () => {
    // The API caps `limit` at 500, so a 1,240-call window is counted from 500
    // rows while the percentages are presented as the window's.
    renderBoth(500, 1240)
    const notes = screen.getAllByText(/Based on the 500 most recent of 1,240 matching calls/)
    expect(notes).toHaveLength(2)
  })

  it("says nothing at exactly the cap when that is the whole window", () => {
    // Boundary: 500 of 500 is complete, not truncated.
    renderBoth(500, 500)
    expect(screen.queryByText(/most recent of/)).toBeNull()
  })

  it("no longer claims the donut covers every call in the window", () => {
    renderBoth(500, 1240)
    expect(screen.queryByText(/Terminal category of every call in this window/)).toBeNull()
  })
})
