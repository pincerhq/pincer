import { act, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { VoiceActiveCall } from "@/api/types"

// The audio pipeline needs a real AudioContext; stub the player and record
// WHEN it gets created — the gesture gate is the thing under test.
const createPlayer = vi.fn()
vi.mock("@/lib/listenInAudio", () => ({
  ListenInPlayer: { create: (...args: unknown[]) => createPlayer(...args) },
}))

vi.mock("@/api/client", () => ({
  getBaseUrl: () => "http://localhost:8080",
  getToken: () => "tok",
}))

import { ListenIn } from "./ListenIn"
import { listenDisabledReason } from "@/lib/listenIn"

class FakeWS {
  static instances: FakeWS[] = []
  readyState = 0
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code?: number }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  closed = false
  constructor(readonly url: string) {
    FakeWS.instances.push(this)
  }
  close() {
    this.closed = true
  }
  serverSend(msg: object) {
    act(() => this.onmessage?.({ data: JSON.stringify(msg) }))
  }
}

function fakePlayer() {
  return {
    analyser: null,
    pushFrame: vi.fn(),
    setTrackMuted: vi.fn(),
    setMasterMuted: vi.fn(),
    onLevels: vi.fn(),
    close: vi.fn(async () => {}),
  }
}

const CALL: VoiceActiveCall = {
  call_sid: "CA1",
  direction: "inbound",
  caller_number: "+4917612345",
  target_number: "",
  target_name: "",
  purpose: "",
  briefing_task_preview: "",
  language: "de",
  engine: "conversation_relay",
  started_at: "2026-08-21T10:00:00+00:00",
  duration_seconds: 12,
  listen_available: true,
  listener_count: 0,
  listener_capacity: 2,
}

describe("ListenIn", () => {
  beforeEach(() => {
    FakeWS.instances = []
    createPlayer.mockReset()
    vi.stubGlobal("WebSocket", FakeWS)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("creates the AudioContext only from the click (gesture-gated start)", async () => {
    const player = fakePlayer()
    createPlayer.mockResolvedValue(player)
    render(<ListenIn call={CALL} />)
    // Rendering alone must not start audio or open a socket.
    expect(createPlayer).not.toHaveBeenCalled()
    expect(FakeWS.instances).toHaveLength(0)

    await userEvent.click(screen.getByRole("button", { name: /listen/i }))
    expect(createPlayer).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(FakeWS.instances).toHaveLength(1))
    expect(FakeWS.instances[0].url).toBe("ws://localhost:8080/api/voice/listen/CA1?token=tok")
    expect(screen.getByTestId("listen-panel")).toBeInTheDocument()
    expect(screen.getByText("Connecting…")).toBeInTheDocument()
  })

  it("shows listener count and meters while listening, forwards frames, mutes per track", async () => {
    const player = fakePlayer()
    createPlayer.mockResolvedValue(player)
    render(<ListenIn call={CALL} />)
    await userEvent.click(screen.getByRole("button", { name: /listen/i }))
    await waitFor(() => expect(FakeWS.instances).toHaveLength(1))
    const ws = FakeWS.instances[0]
    ws.serverSend({ type: "start", call_sid: "CA1", tracks: ["inbound", "outbound"], listener_count: 2 })
    expect(screen.getByText("2 listening")).toBeInTheDocument()
    expect(screen.getByText("Caller")).toBeInTheDocument()
    expect(screen.getByText("Agent")).toBeInTheDocument()
    expect(screen.getAllByRole("meter")).toHaveLength(2)

    ws.serverSend({ type: "media", track: "inbound", payload: "//8=" })
    expect(player.pushFrame).toHaveBeenCalledWith("inbound", "//8=")

    await userEvent.click(screen.getByRole("button", { name: "Mute Caller" }))
    expect(player.setTrackMuted).toHaveBeenCalledWith("inbound", true)
    await userEvent.click(screen.getByRole("button", { name: "Mute" }))
    expect(player.setMasterMuted).toHaveBeenCalledWith(true)
  })

  it("call ended → 'Call ended' → collapses back to the button after 3 s", async () => {
    const player = fakePlayer()
    createPlayer.mockResolvedValue(player)
    render(<ListenIn call={CALL} />)
    await userEvent.click(screen.getByRole("button", { name: /listen/i }))
    await waitFor(() => expect(FakeWS.instances).toHaveLength(1))
    const ws = FakeWS.instances[0]
    ws.serverSend({ type: "start", call_sid: "CA1", tracks: ["inbound", "outbound"], listener_count: 1 })
    ws.serverSend({ type: "end", reason: "call_ended" })
    expect(screen.getByText("Call ended")).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId("listen-panel")).not.toBeInTheDocument(), { timeout: 4500 })
    expect(player.close).toHaveBeenCalled()
    expect(screen.getByRole("button", { name: /listen/i })).toBeInTheDocument()
  }, 8000)

  it("capacity end → 'Listener limit reached'", async () => {
    createPlayer.mockResolvedValue(fakePlayer())
    render(<ListenIn call={CALL} />)
    await userEvent.click(screen.getByRole("button", { name: /listen/i }))
    await waitFor(() => expect(FakeWS.instances).toHaveLength(1))
    FakeWS.instances[0].serverSend({ type: "end", reason: "capacity" })
    expect(screen.getByText("Listener limit reached")).toBeInTheDocument()
  })

  it("stop closes the session and the player", async () => {
    const player = fakePlayer()
    createPlayer.mockResolvedValue(player)
    render(<ListenIn call={CALL} />)
    await userEvent.click(screen.getByRole("button", { name: /listen/i }))
    await waitFor(() => expect(FakeWS.instances).toHaveLength(1))
    FakeWS.instances[0].serverSend({ type: "start", call_sid: "CA1", tracks: ["inbound", "outbound"] })
    await userEvent.click(screen.getByRole("button", { name: /stop/i }))
    expect(FakeWS.instances[0].closed).toBe(true)
    expect(player.close).toHaveBeenCalled()
    expect(screen.queryByTestId("listen-panel")).not.toBeInTheDocument()
  })

  it("button is disabled with a reason when unavailable or at capacity", () => {
    const { rerender } = render(<ListenIn call={{ ...CALL, listen_available: false }} />)
    const btn = screen.getByRole("button", { name: /listen/i })
    expect(btn).toBeDisabled()
    expect(btn.getAttribute("title")).toMatch(/not available/i)
    expect(createPlayer).not.toHaveBeenCalled()

    rerender(<ListenIn call={{ ...CALL, listener_count: 2 }} />)
    const capped = screen.getByRole("button", { name: /listen/i })
    expect(capped).toBeDisabled()
    expect(capped.getAttribute("title")).toMatch(/listener limit reached/i)
    expect(listenDisabledReason({ ...CALL, listener_count: 1 })).toBeNull()
  })
})
