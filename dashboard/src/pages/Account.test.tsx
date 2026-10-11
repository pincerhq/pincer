import type { ReactNode } from "react"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { AccountPage } from "./Account"
import { resetApiClient } from "@/api/client"
import { useAuthStore } from "@/stores/auth"

const BASE = "http://agent.test"
const NO_ACCOUNTS = /Accounts are only available when the agent\s+requires sign-in/

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  })
}

/** Stub `fetch`: `/api/identity/me` answers per test, in order (the last one repeats). */
function stubAgent(me: Array<() => Response>) {
  let asked = 0
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init)
      const path = new URL(request.url).pathname
      if (path === "/api/identity/me") return me[Math.min(asked++, me.length - 1)]()
      return json(404, { detail: "Not found" })
    }),
  )
}

function renderAccount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return render(<AccountPage />, { wrapper })
}

describe("AccountPage", () => {
  beforeEach(() => {
    localStorage.clear()
    useAuthStore.getState().logout()
    useAuthStore.getState().setApiUrl(BASE)
    resetApiClient()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("a failed request while signed in shows the error and a retry, not the no-accounts text", async () => {
    useAuthStore.getState().setSession({
      access_token: "access-1",
      refresh_token: "refresh-1",
      token_type: "bearer",
      expires_in: 1800,
      pincer_user_id: "alice",
    })
    stubAgent([
      () => json(429, { error: "locked_out", detail: "Too many failed authentication attempts" }, { "Retry-After": "120" }),
      () => json(200, { pincer_user_id: "alice", display_name: "Alice", email: null, channels: [] }),
    ])

    renderAccount()

    expect(await screen.findByRole("alert")).toHaveTextContent(/Too many attempts/)
    expect(screen.queryByText(NO_ACCOUNTS)).not.toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }))

    expect(await screen.findByText("Signed in as")).toBeInTheDocument()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })

  it("an agent without sign-in gets the no-accounts text and no retry", async () => {
    useAuthStore.getState().connectWithoutAuth("1.2.3")
    stubAgent([() => json(401, { detail: "No authenticated identity" })])

    renderAccount()

    expect(await screen.findByText(NO_ACCOUNTS)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument()
  })
})
