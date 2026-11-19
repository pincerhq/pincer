// jest-dom's typings only augment the global `jest.Matchers` (and its
// `/vitest` entry targets vitest <5's one-param `Assertion<T>`). vitest 5's
// `Assertion` no longer extends `jest.Matchers`, so add the matchers to
// vitest's own `Matchers` extension point.
import "vitest"
import type { TestingLibraryMatchers } from "@testing-library/jest-dom/matchers"

declare module "vitest" {
  // Type params must match vitest's declaration exactly for merging.
  // eslint-disable-next-line @typescript-eslint/no-empty-object-type, @typescript-eslint/no-unused-vars
  interface Matchers<R extends void | Promise<void> = void | Promise<void>, T = unknown>
    extends TestingLibraryMatchers<unknown, R> {}
}
