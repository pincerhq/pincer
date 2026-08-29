/**
 * Live listen-in player (Sprint 15) — listen-only feed of an active call.
 *
 * The 🎧 button IS the user gesture: the AudioContext is created inside its
 * click handler and never before (browser autoplay policy; no auto-listen).
 * The panel shows Caller / Agent level meters (the real waveform on the mixed
 * output), per-track mute, master mute, stop, and the listener count.
 *
 * Not here on purpose: no record, no download, no barge-in/whisper (the
 * server-side fork is rx-only by protocol), no listening on ended calls.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import { Headphones, Square, Volume2, VolumeX } from "lucide-react"
import type { VoiceActiveCall } from "@/api/types"
import { getBaseUrl, getToken } from "@/api/client"
import { Button } from "@/components/ui/button"
import {
  ListenSession,
  listenDisabledReason,
  listenUrl,
  type ListenState,
  type ListenStateDetail,
  type ListenTrack,
} from "@/lib/listenIn"
import { ListenInPlayer, type TrackLevels } from "@/lib/listenInAudio"
import { cn } from "@/lib/utils"

const ENDED_COLLAPSE_MS = 3000
const TRACK_LABEL: Record<ListenTrack, string> = { inbound: "Caller", outbound: "Agent" }

interface ListenInProps {
  call: VoiceActiveCall
}

/** Button + inline player for one active-call row. */
export function ListenIn({ call }: ListenInProps) {
  const [player, setPlayer] = useState<ListenInPlayer | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const disabledReason = listenDisabledReason(call)

  const handleListen = useCallback(async () => {
    // The AudioContext must be born from this click — nothing else may create it.
    setStarting(true)
    setStartError(null)
    try {
      const created = await ListenInPlayer.create()
      setPlayer(created)
    } catch (err) {
      setStartError(err instanceof Error ? err.message : "Audio could not start")
    } finally {
      setStarting(false)
    }
  }, [])

  const handleClose = useCallback(() => {
    setPlayer((current) => {
      void current?.close()
      return null
    })
  }, [])

  if (player) {
    return <ListenPanel callSid={call.call_sid} player={player} onClose={handleClose} />
  }

  return (
    <div className="flex items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        disabled={!!disabledReason || starting}
        onClick={() => void handleListen()}
        title={disabledReason ?? "Listen to this call live (listen-only)"}
        aria-label="Listen"
        className="border-[var(--color-border)] text-xs"
      >
        <Headphones className="size-3.5" />
        {starting ? "Starting…" : "Listen"}
      </Button>
      {call.listener_count > 0 && (
        <span className="text-[11px] text-[var(--color-muted)]">
          {call.listener_count} listening
        </span>
      )}
      {startError && <span className="text-[11px] text-red-400">{startError}</span>}
    </div>
  )
}

interface ListenPanelProps {
  callSid: string
  player: ListenInPlayer
  onClose: () => void
}

export function ListenPanel({ callSid, player, onClose }: ListenPanelProps) {
  const [state, setState] = useState<ListenState>("connecting")
  const [detail, setDetail] = useState<ListenStateDetail>({})
  const [listenerCount, setListenerCount] = useState(0)
  const [levels, setLevels] = useState<TrackLevels>({ inbound: 0, outbound: 0 })
  const [masterMuted, setMasterMuted] = useState(false)
  const [trackMuted, setTrackMuted] = useState<Record<ListenTrack, boolean>>({
    inbound: false,
    outbound: false,
  })
  const sessionRef = useRef<ListenSession | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  // Connect once per (call, player); stop on unmount.
  useEffect(() => {
    const session = new ListenSession({
      url: listenUrl(getBaseUrl(), callSid, getToken()),
      onState: (next, d) => {
        setState(next)
        setDetail(d)
        if (d.listenerCount != null) setListenerCount(d.listenerCount)
      },
      onFrame: (track, payload) => player.pushFrame(track, payload),
    })
    sessionRef.current = session
    player.onLevels(setLevels)
    session.start()
    return () => {
      player.onLevels(null)
      session.stop()
      sessionRef.current = null
    }
  }, [callSid, player])

  // Call ended → "Call ended" for 3 s → collapse.
  useEffect(() => {
    if (state !== "ended") return
    const timer = setTimeout(onClose, ENDED_COLLAPSE_MS)
    return () => clearTimeout(timer)
  }, [state, onClose])

  // Waveform of the mixed output (AnalyserNode), only while listening.
  useEffect(() => {
    if (state !== "listening") return
    const canvas = canvasRef.current
    const analyser = player.analyser
    if (!canvas || !analyser || typeof analyser.getFloatTimeDomainData !== "function") return
    const ctx = canvas.getContext("2d")
    if (!ctx) return
    const data = new Float32Array(analyser.fftSize)
    let raf = 0
    const draw = () => {
      analyser.getFloatTimeDomainData(data)
      const { width, height } = canvas
      ctx.clearRect(0, 0, width, height)
      ctx.strokeStyle = "rgb(52, 211, 153)"
      ctx.lineWidth = 1.5
      ctx.beginPath()
      for (let i = 0; i < data.length; i++) {
        const x = (i / data.length) * width
        const y = height / 2 - data[i] * (height / 2)
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      ctx.stroke()
      raf = requestAnimationFrame(draw)
    }
    raf = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(raf)
  }, [state, player])

  const toggleMaster = () => {
    const next = !masterMuted
    setMasterMuted(next)
    player.setMasterMuted(next)
  }
  const toggleTrack = (track: ListenTrack) => {
    const next = !trackMuted[track]
    setTrackMuted((m) => ({ ...m, [track]: next }))
    player.setTrackMuted(track, next)
  }
  const stop = () => {
    sessionRef.current?.stop()
    onClose()
  }

  return (
    <div
      data-testid="listen-panel"
      className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] p-3 text-xs"
    >
      <div className="mb-2 flex items-center gap-2">
        <Headphones className="size-3.5 text-emerald-400" />
        <span className="font-medium">Live listen-in</span>
        <span className="font-mono text-[11px] text-[var(--color-muted)]">{callSid}</span>
        <span className="ml-auto text-[11px] text-[var(--color-muted)]">
          {state === "listening" && `${listenerCount} listening`}
          {state === "connecting" && "Connecting…"}
        </span>
      </div>

      {state === "ended" && (
        <p className="py-2 text-center text-[var(--color-muted)]">
          {detail.reason === "unavailable" ? "Call not available for listening" : "Call ended"}
        </p>
      )}
      {state === "capacity" && (
        <div className="flex items-center justify-between py-1">
          <span className="text-amber-400">Listener limit reached</span>
          <Button variant="outline" size="xs" onClick={stop} className="border-[var(--color-border)]">
            Close
          </Button>
        </div>
      )}
      {state === "error" && (
        <div className="flex items-center justify-between py-1">
          <span className="text-red-400">Listen-in connection lost</span>
          <Button variant="outline" size="xs" onClick={stop} className="border-[var(--color-border)]">
            Close
          </Button>
        </div>
      )}

      {(state === "listening" || state === "connecting") && (
        <div className="space-y-2">
          <canvas
            ref={canvasRef}
            width={480}
            height={40}
            className="h-10 w-full rounded bg-black/20"
            aria-hidden="true"
          />
          {(["inbound", "outbound"] as ListenTrack[]).map((track) => (
            <div key={track} className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => toggleTrack(track)}
                title={trackMuted[track] ? `Unmute ${TRACK_LABEL[track]}` : `Mute ${TRACK_LABEL[track]}`}
                aria-label={`${trackMuted[track] ? "Unmute" : "Mute"} ${TRACK_LABEL[track]}`}
                aria-pressed={trackMuted[track]}
                className="flex w-20 items-center gap-1 text-left"
              >
                {trackMuted[track] ? (
                  <VolumeX className="size-3 text-[var(--color-muted)]" />
                ) : (
                  <Volume2 className="size-3" />
                )}
                <span className={cn(trackMuted[track] && "text-[var(--color-muted)] line-through")}>
                  {TRACK_LABEL[track]}
                </span>
              </button>
              <LevelMeter level={levels[track]} muted={trackMuted[track] || masterMuted} />
            </div>
          ))}
          <div className="flex items-center gap-2 pt-1">
            <Button
              variant="outline"
              size="xs"
              onClick={toggleMaster}
              aria-pressed={masterMuted}
              className="border-[var(--color-border)]"
            >
              {masterMuted ? <VolumeX className="size-3" /> : <Volume2 className="size-3" />}
              {masterMuted ? "Unmute" : "Mute"}
            </Button>
            <Button variant="outline" size="xs" onClick={stop} className="ml-auto border-[var(--color-border)]">
              <Square className="size-3" />
              Stop
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

function LevelMeter({ level, muted }: { level: number; muted: boolean }) {
  // RMS of speech sits around 0.05–0.3; stretch it so the bar is readable.
  const pct = Math.min(100, Math.round(Math.sqrt(Math.min(1, level * 3)) * 100))
  return (
    <div
      className="h-2 flex-1 overflow-hidden rounded-full bg-[var(--color-border)]"
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
    >
      <div
        className={cn("h-full transition-[width] duration-75", muted ? "bg-[var(--color-muted)]/40" : "bg-emerald-500")}
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}
