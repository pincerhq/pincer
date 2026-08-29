import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { ListenSession, listenUrl, type ListenState, type ListenStateDetail, type WebSocketLike } from "./listenIn"

class FakeSocket implements WebSocketLike {
  static instances: FakeSocket[] = []
  readyState = 0
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code?: number; reason?: string }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  closedWith: { code?: number; reason?: string } | null = null

  constructor(readonly url: string) {
    FakeSocket.instances.push(this)
  }
  open() {
    this.readyState = 1
    this.onopen?.({})
  }
  serverSend(msg: object) {
    this.onmessage?.({ data: JSON.stringify(msg) })
  }
  serverClose(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code })
  }
  close(code?: number, reason?: string) {
    this.closedWith = { code, reason }
    this.readyState = 3
  }
}

function makeSession(overrides: Partial<ConstructorParameters<typeof ListenSession>[0]> = {}) {
  const states: Array<[ListenState, ListenStateDetail]> = []
  const frames: Array<[string, string]> = []
  const session = new ListenSession({
    url: "ws://x/api/voice/listen/CA1",
    connect: (url) => new FakeSocket(url),
    onState: (s, d) => states.push([s, d]),
    onFrame: (t, p) => frames.push([t, p]),
    reconnectDelayMs: 10,
    ...overrides,
  })
  return { session, states, frames }
}

const START = { type: "start", call_sid: "CA1", tracks: ["inbound", "outbound"], listener_count: 2 }

describe("listenUrl", () => {
  it("switches http(s) to ws(s) and carries the token as a query param", () => {
    expect(listenUrl("https://pincer.example.com/", "CA1", "tok/en")).toBe(
      "wss://pincer.example.com/api/voice/listen/CA1?token=tok%2Fen",
    )
    expect(listenUrl("http://localhost:8080", "CA1", null)).toBe("ws://localhost:8080/api/voice/listen/CA1")
  })
})

describe("ListenSession state machine", () => {
  beforeEach(() => {
    FakeSocket.instances = []
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("connect → start → listening, frames forwarded, call_ended → ended", () => {
    const { session, states, frames } = makeSession()
    session.start()
    expect(session.state).toBe("connecting")
    const ws = FakeSocket.instances[0]
    ws.open()
    expect(session.state).toBe("connecting") // the server's start frame is the real signal
    ws.serverSend(START)
    expect(session.state).toBe("listening")
    expect(session.listenerCount).toBe(2)
    ws.serverSend({ type: "media", track: "inbound", payload: "AAA=" })
    ws.serverSend({ type: "media", track: "outbound", payload: "BBB=" })
    expect(frames).toEqual([
      ["inbound", "AAA="],
      ["outbound", "BBB="],
    ])
    ws.serverSend({ type: "end", reason: "call_ended" })
    expect(session.state).toBe("ended")
    expect(states.at(-1)).toEqual(["ended", { reason: "call_ended" }])
    // the server closes afterwards: no reconnect, state stays ended
    ws.serverClose(1000)
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(1)
    expect(session.state).toBe("ended")
  })

  it("capacity end → capacity state, no reconnect", () => {
    const { session } = makeSession()
    session.start()
    const ws = FakeSocket.instances[0]
    ws.open()
    ws.serverSend({ type: "end", reason: "capacity" })
    ws.serverClose(4001)
    vi.advanceTimersByTime(1000)
    expect(session.state).toBe("capacity")
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it("drop mid-listen → exactly one silent reconnect → then error", () => {
    const { session, states } = makeSession()
    session.start()
    const first = FakeSocket.instances[0]
    first.open()
    first.serverSend(START)
    first.serverClose(1006)
    // silent: still "listening" from the UI's point of view, no state emitted
    expect(session.state).toBe("listening")
    expect(states.filter(([s]) => s === "error")).toHaveLength(0)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(10)
    expect(FakeSocket.instances).toHaveLength(2)
    const second = FakeSocket.instances[1]
    second.open()
    second.serverSend(START)
    expect(session.state).toBe("listening")
    second.serverClose(1006)
    vi.advanceTimersByTime(1000)
    expect(session.state).toBe("error")
    expect(FakeSocket.instances).toHaveLength(2) // no loop
  })

  it("refused before start (no start frame) → error, no reconnect", () => {
    const { session } = makeSession()
    session.start()
    FakeSocket.instances[0].serverClose(1008)
    vi.advanceTimersByTime(1000)
    expect(session.state).toBe("error")
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it("stop() closes the socket and suppresses everything after", () => {
    const { session, frames, states } = makeSession()
    session.start()
    const ws = FakeSocket.instances[0]
    ws.open()
    ws.serverSend(START)
    session.stop()
    expect(ws.closedWith?.code).toBe(1000)
    expect(session.state).toBe("idle")
    const before = states.length
    ws.serverSend({ type: "media", track: "inbound", payload: "x" })
    ws.serverClose(1006)
    vi.advanceTimersByTime(1000)
    expect(frames).toHaveLength(0)
    expect(states.length).toBe(before)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it("ignores media before start and malformed frames", () => {
    const { session, frames } = makeSession()
    session.start()
    const ws = FakeSocket.instances[0]
    ws.onmessage?.({ data: "not json" })
    ws.serverSend({ type: "media", track: "inbound", payload: "early" })
    expect(frames).toHaveLength(0)
    expect(session.state).toBe("connecting")
  })
})
