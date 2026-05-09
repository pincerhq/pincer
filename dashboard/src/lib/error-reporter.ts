function _getAuth(): { baseUrl: string; token: string | null } {
  try {
    const stored = localStorage.getItem("pincer-auth")
    if (!stored) return { baseUrl: window.location.origin, token: null }
    const parsed = JSON.parse(stored)
    const state = parsed?.state
    const url = state?.apiUrl?.trim()
    return {
      baseUrl: url ? url.replace(/\/$/, "") : window.location.origin,
      token: state?.token ?? null,
    }
  } catch {
    return { baseUrl: window.location.origin, token: null }
  }
}

export function reportError(
  error: unknown,
  context?: Record<string, unknown>,
): void {
  const message = error instanceof Error ? error.message : String(error)
  const stack = error instanceof Error ? error.stack : undefined

  console.error("[Pincer]", message, { context, stack })

  const { baseUrl, token } = _getAuth()
  const headers: Record<string, string> = { "Content-Type": "application/json" }
  if (token) headers["Authorization"] = `Bearer ${token}`

  fetch(`${baseUrl}/api/audit/client-errors`, {
    method: "POST",
    headers,
    body: JSON.stringify({ message, stack, context }),
    keepalive: true,
  }).catch(() => {
    // Backend unreachable — already logged to console above
  })
}

export function initGlobalErrorHandlers(): void {
  window.addEventListener("unhandledrejection", (event) => {
    reportError(event.reason, { type: "unhandledrejection" })
  })
  window.addEventListener("error", (event) => {
    if (event.error) {
      reportError(event.error, {
        type: "window.error",
        filename: event.filename,
        lineno: event.lineno,
      })
    }
  })
}
