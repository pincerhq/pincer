# Dashboard Error Handling Guide

## Overview

The dashboard has layered error handling: React error boundaries catch render crashes, React Query surfaces API failures through `isError`, and a central reporter ships everything to the browser console and the backend audit log.

---

## Layers

### 1. React Error Boundaries

`src/components/ErrorBoundary.tsx` is a class component that catches unhandled render exceptions. It is applied to every page route via `PageWrapper` in `App.tsx`.

When an error is caught it:
- Calls `reportError()` to log to the console and POST to `POST /api/audit/client-errors`
- Renders a fallback UI with the error message and a "Try Again" button

Use it to wrap any subtree that should fail in isolation:

```tsx
<ErrorBoundary>
  <SomeRiskyWidget />
</ErrorBoundary>
```

Pass a custom `fallback` prop to override the default UI:

```tsx
<ErrorBoundary fallback={<p>Widget unavailable</p>}>
  <SomeRiskyWidget />
</ErrorBoundary>
```

### 2. Global Window Handlers

`initGlobalErrorHandlers()` in `src/lib/error-reporter.ts` is called once at app boot in `App.tsx`. It registers:
- `window.addEventListener("unhandledrejection", ...)` — catches Promise rejections that escape React Query
- `window.addEventListener("error", ...)` — catches synchronous runtime errors

### 3. API Client Retry (ky)

`src/api/client.ts` uses **ky** with exponential backoff on GET requests:

```
attempt 1 → 300 ms
attempt 2 → 600 ms
attempt 3 → 1200 ms
```

Config: `retry: { limit: 3, methods: ["get"], delay: (n) => 0.3 * 2^(n-1) * 1000 }`

### 4. React Query Retry + QueryCache Logging

`App.tsx` configures the global `QueryClient`:

- `retry: 3` — three attempts before marking a query as failed
- `retryDelay: (n) => min(1000 * 2^n, 30_000)` — exponential, capped at 30 s
- `QueryCache.onError` — calls `reportError()` for every failed query (deduplicated by React Query)
- `MutationCache.onError` — same for mutations

### 5. Per-Hook 404 Handling

Each data hook treats HTTP 404 as "no data yet" (not an error) and returns an empty value so the page renders cleanly:

```ts
queryFn: async () => {
  try {
    return await pincer.someEndpoint()
  } catch (err) {
    if (is404(err)) return emptyFallback   // quiet — resource doesn't exist yet
    throw err                               // propagate → isError: true
  }
}
```

All other status codes are re-thrown, letting React Query set `isError: true` and call `QueryCache.onError`.

### 6. Inline Error Banners (`ApiErrorBanner`)

`src/components/ui/ApiErrorBanner.tsx` is a small component shown inside a page when a query fails:

```tsx
const { data, isError, error, refetch } = useCostsToday()

{isError && (
  <ApiErrorBanner error={error} onRetry={() => refetch()} className="mb-4" />
)}
```

It renders a red alert row with the error message and an optional Retry button. All main pages (Dashboard, Costs, Audit, Conversations, Skills, Doctor, Settings) use it.

### 7. Backend Audit Logging

`POST /api/audit/client-errors` accepts:

```json
{
  "message": "string (max 500 chars)",
  "stack":   "string (max 2000 chars, optional)",
  "context": { "type": "react_error_boundary", ... }
}
```

The endpoint writes an `AuditAction.ERROR` entry with `user_id = "dashboard"` to the SQLite audit log. These entries are visible in the Audit page filtered by `action = error`.

---

## Adding Error Handling to a New Page

1. Destructure `isError`, `error`, and `refetch` from your query hook.
2. Render `<ApiErrorBanner>` above the page content when `isError` is true.
3. For catastrophic failures (blank page, broken layout), wrap the component tree in `<ErrorBoundary>`.

```tsx
import { ApiErrorBanner } from "@/components/ui/ApiErrorBanner"

export function MyPage() {
  const { data, isLoading, isError, error, refetch } = useMyData()

  return (
    <PageContainer title="My Page">
      {isError && <ApiErrorBanner error={error} onRetry={() => refetch()} className="mb-4" />}
      {/* rest of page */}
    </PageContainer>
  )
}
```

## Reporting Errors Manually

Call `reportError()` from anywhere to log to the console and the backend:

```ts
import { reportError } from "@/lib/error-reporter"

try {
  doSomethingRisky()
} catch (err) {
  reportError(err, { context: "my-widget", userId })
}
```

The call is fire-and-forget — it never throws.
