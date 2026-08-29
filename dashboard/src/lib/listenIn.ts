/**
 * Live listen-in session (Sprint 15) — the WebSocket state machine behind the
 * player, kept framework-free so it is unit-testable with a fake socket.
 *
 * Server protocol v1 (JSON text frames, server → client only):
 *   {"type":"start","call_sid":…,"tracks":["inbound","outbound"],"codec":"mulaw","sample_rate":8000,"listener_count":n}
 *   {"type":"media","track":"inbound"|"outbound","payload":"<b64 μ-law>","ts":…}
 *   {"type":"end","reason":"call_ended"|"capacity"|"unavailable"|"error"}
 *
 * Reconnect policy: a socket that drops mid-listen WITHOUT an `end` message
 * gets exactly one silent reconnect attempt; a second drop is an error. No
 * reconnect loop, ever. An `end` message is final.
 */

export type ListenState = "idle" | "connecting" | "listening" | "ended" | "capacity" | "error"
export type ListenTrack = "inbound" | "outbound"

export interface ListenStartMessage {
  type: "start"
  call_sid: string
  tracks: ListenTrack[]
  codec?: string
  sample_rate?: number
  listener_count?: number
}
export interface ListenMediaMessage {
  type: "media"
  track: ListenTrack
  payload: string
  ts?: string | number | null
}
export interface ListenEndMessage {
  type: "end"
  reason: string
}
export type ListenMessage = ListenStartMessage | ListenMediaMessage | ListenEndMessage

/** The subset of WebSocket the session uses — lets tests inject a fake. */
export interface WebSocketLike {
  readyState: number
  onopen: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: { code?: number; reason?: string }) => void) | null
  onerror: ((ev: unknown) => void) | null
  close(code?: number, reason?: string): void
}

export interface ListenStateDetail {
  reason?: string
  listenerCount?: number
}

export interface ListenSessionOptions {
  url: string
  onState: (state: ListenState, detail: ListenStateDetail) => void
  onFrame: (track: ListenTrack, payloadB64: string) => void
  /** Socket factory — defaults to `new WebSocket(url)`. */
  connect?: (url: string) => WebSocketLike
  /** Delay before the single silent reconnect attempt. */
  reconnectDelayMs?: number
}

/** ws(s):// URL of the listener socket for `callSid`, token as a query param
 *  (a browser cannot put an Authorization header on a WebSocket upgrade). */
export function listenUrl(baseUrl: string, callSid: string, token: string | null): string {
  const base = baseUrl.replace(/\/$/, "").replace(/^http/, "ws")
  const qs = token ? `?token=${encodeURIComponent(token)}` : ""
  return `${base}/api/voice/listen/${encodeURIComponent(callSid)}${qs}`
}

/** Why the 🎧 button is disabled for this call, or null when it may be clicked. */
export function listenDisabledReason(call: {
  listen_available: boolean
  listener_count: number
  listener_capacity: number
}): string | null {
  if (!call.listen_available) {
    return "Live listen-in is not available for this call (PINCER_LISTEN_IN_ENABLED off, or the audio fork is not attached yet)"
  }
  if (call.listener_capacity > 0 && call.listener_count >= call.listener_capacity) {
    return `Listener limit reached (${call.listener_count} listening)`
  }
  return null
}

const MAX_RECONNECTS = 1

export class ListenSession {
  state: ListenState = "idle"
  listenerCount = 0
  private ws: WebSocketLike | null = null
  private reconnects = 0
  private stopped = false
  private gotEnd = false
  private wasListening = false
  private timer: ReturnType<typeof setTimeout> | null = null

  constructor(private readonly opts: ListenSessionOptions) {}

  start(): void {
    if (this.state !== "idle") return
    this.setState("connecting", {})
    this.open()
  }

  /** The listener hangs up. Final: no reconnect, no further callbacks. */
  stop(): void {
    this.stopped = true
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
    const ws = this.ws
    this.ws = null
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
      try {
        ws.close(1000, "stopped")
      } catch {
        /* already closed */
      }
    }
    if (this.state !== "ended" && this.state !== "capacity" && this.state !== "error") {
      this.setState("idle", {})
    }
  }

  private setState(state: ListenState, detail: ListenStateDetail): void {
    this.state = state
    if (detail.listenerCount != null) this.listenerCount = detail.listenerCount
    this.opts.onState(state, detail)
  }

  private open(): void {
    const connect = this.opts.connect ?? ((url: string) => new WebSocket(url) as unknown as WebSocketLike)
    let ws: WebSocketLike
    try {
      ws = connect(this.opts.url)
    } catch {
      this.setState("error", { reason: "connect_failed" })
      return
    }
    this.ws = ws
    ws.onopen = () => {
      /* the server's `start` frame is the real "connected" signal */
    }
    ws.onmessage = (ev) => this.handleMessage(ev.data)
    ws.onerror = () => {
      /* onclose follows; decide there */
    }
    ws.onclose = (ev) => this.handleClose(ev?.code ?? 1006)
  }

  private handleMessage(data: unknown): void {
    if (this.stopped) return
    let msg: ListenMessage
    try {
      msg = JSON.parse(String(data)) as ListenMessage
    } catch {
      return
    }
    switch (msg.type) {
      case "start":
        this.wasListening = true
        this.setState("listening", { listenerCount: msg.listener_count ?? 1 })
        break
      case "media":
        if (this.state === "listening" && (msg.track === "inbound" || msg.track === "outbound")) {
          this.opts.onFrame(msg.track, msg.payload)
        }
        break
      case "end":
        this.gotEnd = true
        if (msg.reason === "capacity") this.setState("capacity", { reason: msg.reason })
        else if (msg.reason === "error") this.setState("error", { reason: msg.reason })
        else this.setState("ended", { reason: msg.reason })
        break
      default:
        break
    }
  }

  private handleClose(code: number): void {
    this.ws = null
    if (this.stopped || this.gotEnd) return
    // Dropped without an `end`: one silent retry, then give up.
    if (this.wasListening && this.reconnects < MAX_RECONNECTS) {
      this.reconnects += 1
      this.timer = setTimeout(() => {
        this.timer = null
        if (!this.stopped) this.open()
      }, this.opts.reconnectDelayMs ?? 500)
      return
    }
    this.setState("error", { reason: this.wasListening ? `dropped_${code}` : `refused_${code}` })
  }
}
