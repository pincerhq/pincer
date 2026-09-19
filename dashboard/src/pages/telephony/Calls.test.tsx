import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { MemoryRouter } from "react-router-dom"

const context = { filters: { hours: 24 }, setFilters: vi.fn(), query: { hours: "24" } }

vi.mock("./context", () => ({ useTelephonyContext: () => context }))
vi.mock("@/api/hooks/useTelephony", () => ({ useTelephonyCalls: vi.fn() }))

import { useTelephonyCalls } from "@/api/hooks/useTelephony"
import type { TelephonyFilters } from "@/api/types"
import { TelephonyCalls } from "./Calls"

/** How many calls the current filters match. Narrowing shrinks this. */
let matching = 200

/** Stands in for the API: honours limit/offset and reports the real total. */
function fakeApi(_filters: TelephonyFilters, page: { limit: number; offset: number }) {
  const calls = Array.from({ length: Math.max(0, Math.min(page.limit, matching - page.offset)) }, (_, i) => ({
    call_id: `CA${page.offset + i}`,
    provider_call_id: `CA${page.offset + i}`,
    status: "completed",
  }))
  return { isLoading: false, data: { calls, total: matching, offset: page.offset, limit: page.limit } }
}

function setFilters(next: Record<string, string>) {
  context.filters = { hours: 24, ...next } as TelephonyFilters
  context.query = { hours: "24", ...next }
}

const range = () => screen.getByText(/of \d+$/).textContent

beforeEach(() => {
  matching = 200
  setFilters({})
  vi.mocked(useTelephonyCalls).mockImplementation(fakeApi as never)
})

describe("calls pagination", () => {
  it("pages forward within the current filters", async () => {
    const user = userEvent.setup()
    render(<MemoryRouter><TelephonyCalls /></MemoryRouter>)
    expect(range()).toBe("1–50 of 200")

    await user.click(screen.getByRole("button", { name: "Next" }))
    expect(range()).toBe("51–100 of 200")
  })

  it("returns to the first page when the filters change", async () => {
    const user = userEvent.setup()
    const view = render(<MemoryRouter><TelephonyCalls /></MemoryRouter>)

    await user.click(screen.getByRole("button", { name: "Next" }))
    await user.click(screen.getByRole("button", { name: "Next" }))
    expect(range()).toBe("101–150 of 200")

    // Narrow to something smaller than the current offset. Without the reset
    // the table renders empty at "101–12 of 12", with `Previous` as the only
    // way back.
    matching = 12
    setFilters({ environment: "prod" })
    view.rerender(<MemoryRouter><TelephonyCalls /></MemoryRouter>)

    expect(range()).toBe("1–12 of 12")
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled()
    expect(screen.getAllByRole("row").length).toBeGreaterThan(1)
  })

  it("does not reset the page when the filters are unchanged", async () => {
    const user = userEvent.setup()
    const view = render(<MemoryRouter><TelephonyCalls /></MemoryRouter>)

    await user.click(screen.getByRole("button", { name: "Next" }))
    expect(range()).toBe("51–100 of 200")

    // A re-render with the same filters must not throw the reader back to
    // page one — the reset keys on the filter values, not on render count.
    view.rerender(<MemoryRouter><TelephonyCalls /></MemoryRouter>)
    expect(range()).toBe("51–100 of 200")
  })
})
