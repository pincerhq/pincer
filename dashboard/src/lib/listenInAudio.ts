/**
 * Live listen-in audio pipeline (Sprint 15): μ-law frames → AudioWorklet with
 * a jitter buffer → speakers, with per-track level meters and an
 * AnalyserNode on the mixed output.
 *
 * Browser autoplay policy: `ListenInPlayer.create()` constructs the
 * AudioContext and MUST be called from the user's click — the 🎧 button is
 * the gesture. Nothing here auto-starts audio.
 *
 * Pipeline: main thread decodes μ-law → Float32 (8 kHz) and posts each
 * track's samples to the worklet. The worklet keeps one queue per track,
 * starts playing a track only once `JITTER_MS` of audio is buffered
 * (re-priming after an underrun), linearly resamples 8 kHz → the context
 * rate, mixes Caller + Agent to mono (honouring per-track and master mute)
 * and posts RMS levels ~10×/s. Queues are capped (~3 s) so a stalled tab
 * cannot grow memory or latency without bound.
 */

import { base64ToBytes, decodeMuLaw } from "@/lib/mulaw"
import type { ListenTrack } from "@/lib/listenIn"

export const JITTER_MS = 300
const SOURCE_RATE = 8000
const MAX_QUEUE_SAMPLES = SOURCE_RATE * 3

export interface TrackLevels {
  inbound: number
  outbound: number
}

// Runs inside the AudioWorkletGlobalScope — plain JS, no imports.
const WORKLET_SOURCE = `
class ListenInProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const p = (options && options.processorOptions) || {};
    this.jitter = p.jitterSamples || ${Math.round((JITTER_MS / 1000) * SOURCE_RATE)};
    this.maxQueue = p.maxQueueSamples || ${MAX_QUEUE_SAMPLES};
    this.step = ${SOURCE_RATE} / sampleRate;
    this.tracks = {};
    for (const t of ["inbound", "outbound"]) {
      this.tracks[t] = { chunks: [], pos: 0, queued: 0, primed: false, muted: false, prev: 0, cur: 0, frac: 1, sq: 0 };
    }
    this.master = false;
    this.levelN = 0;
    this.levelEvery = Math.max(1, Math.round(sampleRate / 128 / 10));
    this.port.onmessage = (e) => {
      const m = e.data || {};
      if (m.type === "frame" && this.tracks[m.track]) {
        const t = this.tracks[m.track];
        t.chunks.push(m.samples);
        t.queued += m.samples.length;
        while (t.queued > this.maxQueue && t.chunks.length > 1) {
          const dropped = t.chunks.shift();
          t.queued -= dropped.length - t.pos;
          t.pos = 0;
        }
      } else if (m.type === "mute" && this.tracks[m.track]) {
        this.tracks[m.track].muted = !!m.muted;
      } else if (m.type === "master") {
        this.master = !!m.muted;
      }
    };
  }
  _next(t) {
    if (!t.primed) {
      if (t.queued >= this.jitter) t.primed = true;
      else return 0;
    }
    while (t.chunks.length && t.pos >= t.chunks[0].length) {
      t.chunks.shift();
      t.pos = 0;
    }
    if (!t.chunks.length) {
      t.primed = false; // underrun: wait for the jitter buffer to refill
      t.queued = 0;
      return 0;
    }
    const s = t.chunks[0][t.pos++];
    t.queued--;
    return s;
  }
  _sample(t) {
    // Linear interpolation 8 kHz -> sampleRate.
    t.frac += this.step;
    while (t.frac >= 1) {
      t.prev = t.cur;
      t.cur = this._next(t);
      t.frac -= 1;
    }
    return t.prev + (t.cur - t.prev) * t.frac;
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!out) return true;
    const a = this.tracks.inbound, b = this.tracks.outbound;
    for (let i = 0; i < out.length; i++) {
      const sa = this._sample(a), sb = this._sample(b);
      a.sq += sa * sa; b.sq += sb * sb;
      let mix = (a.muted ? 0 : sa) + (b.muted ? 0 : sb);
      if (this.master) mix = 0;
      out[i] = mix > 1 ? 1 : mix < -1 ? -1 : mix;
    }
    this.levelN++;
    if (this.levelN >= this.levelEvery) {
      const n = this.levelN * out.length;
      this.port.postMessage({ type: "levels", inbound: Math.sqrt(a.sq / n), outbound: Math.sqrt(b.sq / n) });
      a.sq = 0; b.sq = 0; this.levelN = 0;
    }
    return true;
  }
}
registerProcessor("listen-in-processor", ListenInProcessor);
`

export class ListenInPlayer {
  private levelsCb: ((levels: TrackLevels) => void) | null = null
  private closed = false

  private constructor(
    private readonly ctx: AudioContext,
    private readonly node: AudioWorkletNode,
    readonly analyser: AnalyserNode,
  ) {
    node.port.onmessage = (ev: MessageEvent) => {
      const m = ev.data as { type?: string; inbound?: number; outbound?: number }
      if (m?.type === "levels" && this.levelsCb) {
        this.levelsCb({ inbound: m.inbound ?? 0, outbound: m.outbound ?? 0 })
      }
    }
  }

  /** MUST be called from a user gesture (the 🎧 click). */
  static async create(): Promise<ListenInPlayer> {
    const ctx = new AudioContext()
    const blob = new Blob([WORKLET_SOURCE], { type: "application/javascript" })
    const url = URL.createObjectURL(blob)
    try {
      await ctx.audioWorklet.addModule(url)
    } finally {
      URL.revokeObjectURL(url)
    }
    const node = new AudioWorkletNode(ctx, "listen-in-processor", {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    })
    const analyser = ctx.createAnalyser()
    analyser.fftSize = 1024
    node.connect(analyser)
    analyser.connect(ctx.destination)
    if (ctx.state === "suspended") await ctx.resume()
    return new ListenInPlayer(ctx, node, analyser)
  }

  pushFrame(track: ListenTrack, payloadB64: string): void {
    if (this.closed) return
    const samples = decodeMuLaw(base64ToBytes(payloadB64))
    this.node.port.postMessage({ type: "frame", track, samples }, [samples.buffer])
  }

  setTrackMuted(track: ListenTrack, muted: boolean): void {
    if (!this.closed) this.node.port.postMessage({ type: "mute", track, muted })
  }

  setMasterMuted(muted: boolean): void {
    if (!this.closed) this.node.port.postMessage({ type: "master", muted })
  }

  onLevels(cb: ((levels: TrackLevels) => void) | null): void {
    this.levelsCb = cb
  }

  async close(): Promise<void> {
    if (this.closed) return
    this.closed = true
    this.levelsCb = null
    try {
      this.node.port.onmessage = null
      this.node.disconnect()
      this.analyser.disconnect()
    } catch {
      /* already torn down */
    }
    try {
      await this.ctx.close()
    } catch {
      /* already closed */
    }
  }
}
