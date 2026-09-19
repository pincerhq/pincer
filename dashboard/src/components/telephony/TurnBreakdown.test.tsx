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
  it("names the engine's own stop boundary and not the definition's start fallback", () => {
    // `limitations` also describes the ConversationRelay START fallback, which
    // contradicts a speech_end turn, so it is not rendered here.
    expand("conversation_relay", "SENTINEL-start-falls-back.")
    const paragraph = screen.getByText(/Clock starts at/).textContent ?? ""
    expect(paragraph).toContain("first response text token was")
    expect(paragraph).not.toContain("SENTINEL")
  })

  it("does not guess a stop boundary for an engine it has never heard of", () => {
    // Previously anything that was not "conversation_relay" rendered "audio".
    expand("some_future_engine", "SENTINEL.")
    const paragraph = screen.getByText(/Clock starts at/).textContent ?? ""
    expect(paragraph).toContain("first response was written to the provider")
    expect(paragraph).not.toMatch(/text token|response audio/)
  })

  it("still says which start boundary this particular turn used", () => {
    // Per-turn data a metric definition cannot know, so it stays local.
    expand("media_streams", "SENTINEL.", "transcript_arrival")
    expect(screen.getByText(/transcript arrival/)).toBeInTheDocument()
  })

  it("keeps both boundaries and the caveat when definitions have not loaded", () => {
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
    // The caveat must not wait on a second query.
    expect(screen.getByText(/SENT, not heard/)).toBeInTheDocument()
  })
})
