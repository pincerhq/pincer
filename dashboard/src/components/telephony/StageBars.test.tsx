import { render, screen } from "@testing-library/react"
import { describe, it, expect } from "vitest"
import { MemoryRouter } from "react-router-dom"
import { StageBars } from "./StageBars"

function summary(p50: number | null, p95: number | null, count = 40) {
  return {
    count,
    p50,
    p95,
    p99: p95,
    min: p50,
    max: p95,
    mean: p50,
    sufficient_samples: true,
    min_samples: 20,
  }
}

function renderBars(stages: Record<string, ReturnType<typeof summary>>) {
  const data = { stages, unavailable: [] }
  return render(
    <MemoryRouter>
      <StageBars
        data={data as never}
        definitions={[]}
        selected=""
        onSelect={() => {}}
        query={{}}
      />
    </MemoryRouter>,
  )
}

/** The legend renders one button per charted stage. */
const charted = () =>
  screen
    .getAllByRole("button")
    .map((b) => b.textContent ?? "")
    .filter((text) => text.includes("n="))

describe("stage bars and missing measurements", () => {
  it("charts a stage that has percentiles", () => {
    renderBars({ llm_total_ms: summary(120, 400) })
    expect(charted().some((t) => t.includes("LLM generation"))).toBe(true)
    expect(screen.queryByText(/Not measured:/)).toBeNull()
  })

  it("does not draw a bar for a stage with no measured percentile", () => {
    // `?? 0` turned this into a full-length-scale 0ms bar that read as the
    // fastest stage on the chart. `Ms` in ./shared states the rule: a measured
    // number and a missing one must never look alike.
    renderBars({
      llm_total_ms: summary(120, 400),
      tts_total_ms: summary(null, null),
    })

    expect(charted().some((t) => t.includes("LLM generation"))).toBe(true)
    expect(charted().some((t) => t.includes("TTS"))).toBe(false)
    expect(screen.getByText(/Not measured:/)).toBeInTheDocument()
  })

  it("still charts a genuine 0ms measurement", () => {
    // The guard must key on null, not on falsiness — a real zero is a
    // measurement and dropping it would be the same class of lie in reverse.
    renderBars({ agent_queue_ms: summary(0, 0) })
    expect(charted().some((t) => t.includes("Agent queue"))).toBe(true)
    expect(screen.queryByText(/Not measured:/)).toBeNull()
  })
})
