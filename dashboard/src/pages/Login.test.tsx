import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { LoginPage } from "./Login"
import { useAuthStore } from "@/stores/auth"

const BASE = "http://agent.test"

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  })
}

/** Stub `fetch`: health and login answers are per test. */
function stubAgent(opts: { login: () => Response; authRequired?: boolean }) {
  const logins: unknown[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(input, init)
      const path = new URL(request.url).pathname
      if (path === "/api/health") {
        return json(200, { status: "ok", version: "1.2.3", auth_required: opts.authRequired ?? true })
      }
      if (path === "/api/auth/login") {
        logins.push(await request.json())
        return opts.login()
      }
      return json(404, { detail: "Not found" })
    }),
  )
  return logins
}

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<p>home page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

async function submit(identifier: string, password: string) {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText("Name or email"), identifier)
  await user.type(screen.getByLabelText("Password"), password)
  await user.click(screen.getByRole("button", { name: "Sign in" }))
}

describe("LoginPage", () => {
  beforeEach(() => {
    localStorage.clear()
    useAuthStore.getState().logout()
    useAuthStore.getState().setApiUrl(BASE)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("successful login stores the session and navigates home", async () => {
    const logins = stubAgent({
      login: () =>
        json(200, {
          access_token: "access-1",
          refresh_token: "refresh-1",
          token_type: "bearer",
          expires_in: 1800,
          pincer_user_id: "alice",
        }),
    })
    renderLogin()
    await submit("alice@example.com", "s3cret-pass")

    expect(await screen.findByText("home page")).toBeInTheDocument()
    expect(logins).toEqual([{ identifier: "alice@example.com", password: "s3cret-pass" }])
    const state = useAuthStore.getState()
    expect(state.accessToken).toBe("access-1")
    expect(state.refreshToken).toBe("refresh-1")
    expect(state.user).toBe("alice")
    expect(state.isConnected).toBe(true)
    expect(state.version).toBe("1.2.3")
    expect(state.apiUrl).toBe(BASE)
  })

  it("401 shows the invalid-credentials message and stores nothing", async () => {
    stubAgent({ login: () => json(401, { error: "invalid_credentials", detail: "Invalid credentials" }) })
    renderLogin()
    await submit("alice", "wrong")

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid name, email or password.")
    expect(useAuthStore.getState().accessToken).toBeNull()
    expect(useAuthStore.getState().isConnected).toBe(false)
    expect(screen.queryByText("home page")).not.toBeInTheDocument()
  })

  it("429 shows the lockout message with the wait in minutes", async () => {
    stubAgent({
      login: () => json(429, { error: "locked_out", detail: "Locked" }, { "Retry-After": "840" }),
    })
    renderLogin()
    await submit("alice", "wrong")

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many failed attempts. Try again in 14 minutes.",
    )
  })

  it("429 with a short wait is spelled out in seconds", async () => {
    stubAgent({
      login: () => json(429, { error: "locked_out", detail: "Locked" }, { "Retry-After": "45" }),
    })
    renderLogin()
    await submit("alice", "wrong")

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many failed attempts. Try again in 45 seconds.",
    )
  })

  it("offers no bypass when the agent requires sign-in", async () => {
    stubAgent({ login: () => json(401, {}) })
    renderLogin()
    await submit("alice", "wrong")
    await screen.findByRole("alert")
    expect(screen.queryByRole("button", { name: "Continue without signing in" })).not.toBeInTheDocument()
  })

  it("auth disabled → 'Continue without signing in' connects without tokens", async () => {
    stubAgent({ login: () => json(401, {}), authRequired: false })
    renderLogin()

    await userEvent.click(await screen.findByRole("button", { name: "Continue without signing in" }))

    expect(await screen.findByText("home page")).toBeInTheDocument()
    const state = useAuthStore.getState()
    expect(state.isConnected).toBe(true)
    expect(state.authRequired).toBe(false)
    expect(state.accessToken).toBeNull()
  })

  it("no longer mentions the shared dashboard token", () => {
    stubAgent({ login: () => json(401, {}) })
    const { container } = renderLogin()
    expect(container.textContent).not.toMatch(/DASHBOARD_TOKEN/)
    expect(container.textContent).toContain("pincer identity set-password <name>")
  })
})
