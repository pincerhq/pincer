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
