import { render, screen } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { MemoryRouter, Route, Routes } from "react-router-dom"

vi.mock("@/api/hooks/useTelephony", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/hooks/useTelephony")>()
  return {
    ...actual,
    useTelephonyAlerts: vi.fn(() => ({ data: [] })),
    useTelephonyCalls: vi.fn(),
  }
})

import { useTelephonyCalls } from "@/api/hooks/useTelephony"
import type { TelephonyFilters } from "@/api/types"
import { TelephonyLayout } from "./Layout"

/** Three environments, two models — enough for a facet to hide another. */
const CORPUS = [
  { call_id: "1", environment: "prod", app_version: "1.0.0", model: "haiku", language: "en" },
  { call_id: "2", environment: "staging", app_version: "1.1.0", model: "haiku", language: "de" },
  { call_id: "3", environment: "dev", app_version: "1.1.0", model: "sonnet", language: "en" },
]

/** Stands in for the API: returns only the rows matching the filters it is given. */
function fakeApi(filters: TelephonyFilters) {
  const rows = CORPUS.filter((row) =>
    (["environment", "app_version", "model", "language"] as const).every(
      (key) => !filters[key] || row[key] === filters[key],
    ),
  )
  return { data: { calls: rows } }
}

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/telephony${search}`]}>
      <Routes>
        <Route path="/telephony" element={<TelephonyLayout />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** The values offered by the <select> whose placeholder reads "<label>: any". */
function optionsOf(label: string): string[] {
  const selects = screen.getAllByRole("combobox") as HTMLSelectElement[]
  const target = selects.find((el) => el.options[0]?.text === `${label}: any`)
  if (!target) throw new Error(`no select for ${label}`)
  return [...target.options].slice(1).map((option) => option.value)
}

beforeEach(() => {
  vi.mocked(useTelephonyCalls).mockImplementation(((filters: TelephonyFilters) =>
    fakeApi(filters)) as never)
})

describe("facet dropdowns", () => {
  it("offers every value that exists when nothing is selected", () => {
    renderAt("")
    expect(optionsOf("Environment")).toEqual(["dev", "prod", "staging"])
  })

  it("keeps the other values of a facet reachable after selecting one", () => {
    // The bug: the option lists were derived from the fully-filtered rows, so
    // environment=prod returned only prod rows and the environment dropdown
    // collapsed to ["prod"] — a one-way door out of which the only exit was
    // clearing every filter.
    renderAt("?environment=prod")
    expect(optionsOf("Environment")).toEqual(["dev", "prod", "staging"])
  })

  it("still narrows a facet by the OTHER facets, so the offer stays honest", () => {
    // model=sonnet exists only on the dev call, so dev is the only environment
    // worth offering. Excluding a facet from its own list must not turn every
    // list into the unfiltered set.
    renderAt("?model=sonnet")
    expect(optionsOf("Environment")).toEqual(["dev"])
  })

  it("keeps the current selection visible even when other facets exclude it", () => {
    // prod has no sonnet call, so the environment query returns no prod row.
    // Without re-adding the selection the native <select> would render blank
    // and the user could not see or clear what is filtering.
    renderAt("?model=sonnet&environment=prod")
    expect(optionsOf("Environment")).toContain("prod")
  })
})
