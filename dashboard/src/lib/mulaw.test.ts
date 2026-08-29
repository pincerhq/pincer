import { describe, expect, it } from "vitest"
import { MULAW_TO_FLOAT, MULAW_TO_PCM16, base64ToBytes, decodeMuLaw, rms } from "./mulaw"

describe("μ-law decode table", () => {
  // Known G.711 vectors (byte → 16-bit PCM).
  it.each([
    [0x00, -32124],
    [0x80, 32124],
    [0xff, 0],
    [0x7f, 0], // negative zero
    [0xf0, 120],
    [0x70, -120],
    [0x0f, -16764],
    [0x8f, 16764],
  ])("byte 0x%s decodes to %i", (byte, pcm) => {
    expect(MULAW_TO_PCM16[byte as number]).toBe(pcm)
  })

  it("is sign-symmetric: flipping the sign bit negates the sample", () => {
    for (let i = 0; i < 256; i++) {
      expect(MULAW_TO_PCM16[i] + MULAW_TO_PCM16[i ^ 0x80]).toBe(0)
    }
  })

  it("float table stays inside [-1, 1)", () => {
    for (let i = 0; i < 256; i++) {
      expect(MULAW_TO_FLOAT[i]).toBeGreaterThanOrEqual(-1)
      expect(MULAW_TO_FLOAT[i]).toBeLessThan(1)
    }
    expect(MULAW_TO_FLOAT[0xff]).toBe(0)
    expect(MULAW_TO_FLOAT[0x80]).toBeCloseTo(32124 / 32768, 6)
  })

  it("decodes a frame byte by byte", () => {
    const out = decodeMuLaw(new Uint8Array([0xff, 0x80, 0x00]))
    expect(Array.from(out)).toEqual([0, MULAW_TO_FLOAT[0x80], MULAW_TO_FLOAT[0x00]])
  })
})

describe("base64ToBytes", () => {
  it("round-trips Twilio-style payloads", () => {
    const bytes = new Uint8Array([0, 1, 2, 0x7f, 0x80, 0xff])
    const b64 = btoa(String.fromCharCode(...bytes))
    expect(Array.from(base64ToBytes(b64))).toEqual(Array.from(bytes))
  })
})

describe("rms", () => {
  it("is 0 for silence and the amplitude for a square wave", () => {
    expect(rms(new Float32Array(160))).toBe(0)
    const square = new Float32Array(160).map((_, i) => (i % 2 ? 0.5 : -0.5))
    expect(rms(square)).toBeCloseTo(0.5, 6)
    expect(rms(new Float32Array(0))).toBe(0)
  })
})
