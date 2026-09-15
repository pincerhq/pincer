import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { Link, MemoryRouter, Route, Routes } from "react-router-dom"

vi.mock("@/api/hooks/useTelephony", () => ({
  useTelephonyCall: vi.fn(),
  useTelephonyCallEvents: vi.fn(),
  useTelephonyCallSpans: vi.fn(),
  useTelephonyMetricDefinitions: vi.fn(),
}))

import {
  useTelephonyCall,
  useTelephonyCallEvents,
  useTelephonyCallSpans,
  useTelephonyMetricDefinitions,
} from "@/api/hooks/useTelephony"
import { TelephonyCallPage } from "./TelephonyCall"

/** One turn per call, with ids that cannot be confused between calls. */
function detailFor(ref: string) {
  return {
    call: { call_id: ref, provider_call_id: ref, status: "completed" },
    turns: [{ turn_id: `${ref}_T1`, turn_no: 1, response_latency_ms: 900 }],
    unavailable: [],
    telemetry_gaps: [],
  }
}

/**
 * Renders the page under a router that STAYS MOUNTED across the navigation.
 *
 * This is the whole point: unmounting between renders would destroy the state
 * on its own and the assertions would hold with or without the fix. The links
 * live outside <Routes> so clicking one changes the params while React keeps
 * reconciling the same element — exactly what the router does in the app.
 */
function renderAt(path: string, links: string[] = []) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      {links.map((to) => (
        <Link key={to} to={to}>
          go {to}
        </Link>
      ))}
      <Routes>
        <Route path="/telephony/calls/:callRef" element={<TelephonyCallPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.mocked(useTelephonyCallEvents).mockReturnValue({ data: [] } as never)
  vi.mocked(useTelephonyCallSpans).mockReturnValue({ data: [] } as never)
  vi.mocked(useTelephonyMetricDefinitions).mockReturnValue({ data: [] } as never)
  vi.mocked(useTelephonyCall).mockImplementation(
    ((ref: string) => ({ isLoading: false, data: detailFor(ref) })) as never,
  )
})

describe("call detail selection state", () => {
  it("seeds the selected turn from ?turn=", () => {
    renderAt("/telephony/calls/CA_A?turn=CA_A_T1")
    expect(screen.getByText("Waterfall · selected turn")).toBeInTheDocument()
  })

  it("does not carry a turn selection onto a different call", async () => {
    // `AlertStrip` links to a call WITHOUT a turn param. The route renders one
    // element per :callRef, so without a remount the detail keeps the previous
    // call's `selectedTurn` and filters the waterfall by a turn id that is not
    // in this call — with nothing in the URL to explain why.
    const user = userEvent.setup()
    renderAt("/telephony/calls/CA_A?turn=CA_A_T1", ["/telephony/calls/CA_B"])
    expect(screen.getByText("Waterfall · selected turn")).toBeInTheDocument()

    await user.click(screen.getByRole("link", { name: "go /telephony/calls/CA_B" }))

    expect(screen.getByText("Waterfall · whole call")).toBeInTheDocument()
  })

  it("re-seeds when only the requested turn changes on the call already open", async () => {
    const user = userEvent.setup()
    renderAt("/telephony/calls/CA_A", ["/telephony/calls/CA_A?turn=CA_A_T1"])
    expect(screen.getByText("Waterfall · whole call")).toBeInTheDocument()

    await user.click(screen.getByRole("link", { name: "go /telephony/calls/CA_A?turn=CA_A_T1" }))

    expect(screen.getByText("Waterfall · selected turn")).toBeInTheDocument()
  })
})
