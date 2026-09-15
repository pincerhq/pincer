import { render, screen } from "@testing-library/react"
import { describe, it, expect } from "vitest"
import { TurnBreakdown } from "./TurnBreakdown"
import type { TelephonyMetricDefinition, TelephonyTurn } from "@/api/types"

function turn(engine: string, source: string): TelephonyTurn {
  return {
    turn_id: "T1",
    call_id: "CA1",
    turn_no: 1,
    engine,
    response_latency_ms: 900,
    response_latency_source: source,
    critical_path: [],
    complete: true,
    interrupted: false,
    cancelled: false,
  } as unknown as TelephonyTurn
}

/** A definition whose wording is unmistakably the backend's, not the JSX's. */
function definition(limitations: string): TelephonyMetricDefinition {
  return {
    key: "response_latency_ms",
    label: "Response latency (audio sent)",
    start_event: "stt.speech_end",
    end_event: "audio.dispatched",
    source: "server_measured",
    limitations,
    available_on: ["conversation_relay", "media_streams"],
    unavailable_reason: "",
  } as unknown as TelephonyMetricDefinition
}

/** The panel is parent-controlled, so selecting the turn is what expands it. */
function expand(engine: string, limitations: string, source = "speech_end") {
  render(
    <TurnBreakdown
      turns={[turn(engine, source)]}
      definitions={[definition(limitations)]}
      selectedTurnId="T1"
      onSelect={() => {}}
      engine={engine}
    />,
  )
}

describe("turn breakdown boundary wording", () => {
  it("takes the stop boundary from the metric definition, not from the engine name", () => {
    // The whole point: change schema.py's wording and the UI follows, with no
    // engine string to hunt for here.
    expand("conversation_relay", "SENTINEL-ends-at-the-first-text-token.")
    expect(screen.getByText(/SENTINEL-ends-at-the-first-text-token/)).toBeInTheDocument()
  })

  it("uses that same definition for an engine it has never heard of", () => {
    // Adding an engine must not require a code change here. Previously anything
    // that was not "conversation_relay" silently rendered "audio".
    expand("some_future_engine", "SENTINEL-ends-when-written-to-the-provider.")

    // Scoped to the boundary paragraph: the panel has an unrelated
    // "Critical path to first response audio" heading.
    const paragraph = screen.getByText(/Clock starts at/).textContent ?? ""
    expect(paragraph).toContain("SENTINEL-ends-when-written-to-the-provider")
    expect(paragraph).not.toContain("text token")
  })

  it("still says which start boundary this particular turn used", () => {
    // Per-turn data a metric definition cannot know, so it stays local.
    expand("media_streams", "SENTINEL.", "transcript_arrival")
    expect(screen.getByText(/transcript arrival/)).toBeInTheDocument()
  })

  it("degrades to the per-turn sentence when definitions have not loaded", () => {
    render(
      <TurnBreakdown
        turns={[turn("media_streams", "speech_end")]}
        definitions={[]}
        selectedTurnId="T1"
        onSelect={() => {}}
        engine="media_streams"
      />,
    )
    expect(screen.getByText(/measured speech end/)).toBeInTheDocument()
  })
})
