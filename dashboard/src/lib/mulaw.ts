/**
 * G.711 μ-law decoding for the live listen-in player (Sprint 15).
 *
 * Twilio media frames are base64 μ-law at 8 kHz. Decoding is a 256-entry
 * lookup (built once) — the server relays frames untranscoded on purpose.
 */

const BIAS = 0x84

function decodeOne(byte: number): number {
  const u = ~byte & 0xff
  const sign = u & 0x80
  const exponent = (u >> 4) & 0x07
  const mantissa = u & 0x0f
  let sample = ((mantissa << 3) + BIAS) << exponent
  sample -= BIAS
  return sign ? -sample : sample
}

/** μ-law byte → 16-bit PCM sample (−32124 … 32124). */
export const MULAW_TO_PCM16: Int16Array = (() => {
  const table = new Int16Array(256)
  for (let i = 0; i < 256; i++) table[i] = decodeOne(i)
  return table
})()

/** μ-law byte → Float32 in [−1, 1). */
export const MULAW_TO_FLOAT: Float32Array = (() => {
  const table = new Float32Array(256)
  for (let i = 0; i < 256; i++) table[i] = MULAW_TO_PCM16[i] / 32768
  return table
})()

export function decodeMuLaw(bytes: Uint8Array): Float32Array {
  const out = new Float32Array(bytes.length)
  for (let i = 0; i < bytes.length; i++) out[i] = MULAW_TO_FLOAT[bytes[i]]
  return out
}

export function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

/** Root-mean-square level of a PCM float buffer (0 … 1). */
export function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0
  let acc = 0
  for (let i = 0; i < samples.length; i++) acc += samples[i] * samples[i]
  return Math.sqrt(acc / samples.length)
}
