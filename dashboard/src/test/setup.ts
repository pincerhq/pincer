import "@testing-library/jest-dom"
import { vi } from "vitest"

// IntersectionObserver is not available in jsdom
class MockIntersectionObserver {
  observe = vi.fn()
  unobserve = vi.fn()
  disconnect = vi.fn()
  constructor(_: IntersectionObserverCallback) {}
}
vi.stubGlobal("IntersectionObserver", MockIntersectionObserver)

// Node 25 defines its own `localStorage` global, which shadows jsdom's and,
// without a valid --localstorage-file, is undefined or an object with no
// working methods. Give tests an in-memory one unless a usable one is there.
if (typeof globalThis.localStorage?.getItem !== "function") {
  class MemoryStorage implements Storage {
    private items = new Map<string, string>()
    get length() {
      return this.items.size
    }
    clear() {
      this.items.clear()
    }
    getItem(key: string) {
      return this.items.get(key) ?? null
    }
    key(index: number) {
      return [...this.items.keys()][index] ?? null
    }
    removeItem(key: string) {
      this.items.delete(key)
    }
    setItem(key: string, value: string) {
      this.items.set(key, String(value))
    }
  }
  // Not vi.stubGlobal: a test's vi.unstubAllGlobals() would take it away again.
  Object.defineProperty(globalThis, "localStorage", {
    value: new MemoryStorage(),
    configurable: true,
    writable: true,
  })
}
