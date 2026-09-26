import { describe, expect, it } from "vitest"
import { shortId } from "./formatters"

describe("shortId", () => {
  it("distinguishes two ids minted in the same millisecond", () => {
    // Real UUIDv7s differing only after the timestamp and counter.
    const a = "01a08c3f-1234-7000-8000-0000aaaaaaaa"
    const b = "01a08c3f-1234-7000-8000-0000bbbbbbbb"
    expect(shortId(a)).not.toEqual(shortId(b))
  })

  it("leaves a short value alone", () => {
    expect(shortId("CA123")).toEqual("CA123")
  })
})
