import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { HTTPError } from "ky"
import { authRedirect, pincer, resetApiClient } from "./client"
import { useAuthStore } from "@/stores/auth"

const BASE = "http://agent.test"

function json(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  })
}

function pair(n: number) {
  return {
    access_token: `access-${n}`,
    refresh_token: `refresh-${n}`,
    token_type: "bearer",
    expires_in: 1800,
    pincer_user_id: "alice",
  }
}

interface Call {
  path: string
  method: string
  auth: string | null
  body: string
}

/**
 * Stub `fetch` with a server that accepts exactly one access token. Everything
 * else gets `401 token_expired`; the routes below can be overridden per test.
 */
function stubServer(opts: {
  validToken: string
  refresh?: () => Response | Promise<Response>
  routes?: Record<string, () => Response>
}) {
  const calls: Call[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init)
    const path = new URL(request.url).pathname
    const call = {
      path,
      method: request.method,
      auth: request.headers.get("Authorization"),
      body: await request.text(),
    }
    calls.push(call)
    if (path === "/api/auth/refresh") {
      return opts.refresh ? opts.refresh() : json(401, { error: "invalid_token", detail: "bad" })
    }
    if (opts.routes?.[path]) return opts.routes[path]()
    if (call.auth !== `Bearer ${opts.validToken}`) {
      return json(401, { error: "token_expired", detail: "Token expired" })
    }
    return json(200, { ok: path })
  })
  vi.stubGlobal("fetch", fetchMock)
  return { calls, to: (path: string) => calls.filter((c) => c.path === path) }
}

function signIn() {
  useAuthStore.getState().setApiUrl(BASE)
  useAuthStore.getState().setSession(pair(1))
}

describe("api client token refresh", () => {
  let redirect: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    localStorage.clear()
    useAuthStore.getState().logout()
    resetApiClient()
    redirect = vi.spyOn(authRedirect, "go").mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("401 token_expired → one refresh → request retried with the new token", async () => {
    signIn()
    const server = stubServer({ validToken: "access-2", refresh: () => json(200, pair(2)) })

    await expect(pincer.status()).resolves.toEqual({ ok: "/api/status" })

    expect(server.to("/api/auth/refresh")).toHaveLength(1)
    expect(JSON.parse(server.to("/api/auth/refresh")[0].body)).toEqual({ refresh_token: "refresh-1" })
    expect(server.to("/api/status").map((c) => c.auth)).toEqual(["Bearer access-1", "Bearer access-2"])
    const state = useAuthStore.getState()
    expect(state.accessToken).toBe("access-2")
    expect(state.refreshToken).toBe("refresh-2")
    expect(state.isConnected).toBe(true)
    expect(JSON.parse(localStorage.getItem("pincer-auth")!).state.accessToken).toBe("access-2")
    expect(redirect).not.toHaveBeenCalled()
  })

  it("the retry carries the original method and body", async () => {
    signIn()
    const server = stubServer({ validToken: "access-2", refresh: () => json(200, pair(2)) })

    await pincer.changePassword("old-pass", "new-pass")

    const puts = server.to("/api/identity/me/password")
    expect(puts.map((c) => c.method)).toEqual(["PUT", "PUT"])
    expect(JSON.parse(puts[1].body)).toEqual({ current_password: "old-pass", new_password: "new-pass" })
  })

  it("concurrent 401s share exactly one refresh", async () => {
    signIn()
    let release!: (response: Response) => void
    const server = stubServer({
      validToken: "access-2",
      refresh: () => new Promise<Response>((resolve) => (release = resolve)),
    })

    const all = Promise.all([pincer.status(), pincer.costsToday(), pincer.skills(), pincer.me()])
    // Every request has had its 401 and is waiting on the same refresh.
    await vi.waitFor(() => expect(server.calls.filter((c) => c.auth === "Bearer access-1")).toHaveLength(4))
    await vi.waitFor(() => expect(server.to("/api/auth/refresh")).toHaveLength(1))
    release(json(200, pair(2)))
    await all

    expect(server.to("/api/auth/refresh")).toHaveLength(1)
    expect(server.calls.filter((c) => c.auth === "Bearer access-2")).toHaveLength(4)
  })

  it("a 401 that arrives after someone else refreshed retries without a second refresh", async () => {
    signIn()
    let first = true
    const server = stubServer({
      validToken: "access-2",
      routes: {
        "/api/status": () => {
          if (!first) return json(200, { ok: true })
          // While this request was in the air, another one stored a new pair.
          first = false
          useAuthStore.getState().setSession(pair(2))
          return json(401, { error: "token_expired", detail: "Token expired" })
        },
      },
    })

    await expect(pincer.status()).resolves.toEqual({ ok: true })

    expect(server.to("/api/status").map((c) => c.auth)).toEqual(["Bearer access-1", "Bearer access-2"])
    expect(server.to("/api/auth/refresh")).toHaveLength(0)
  })

  it("refresh rejected → session cleared and redirect to /login", async () => {
    signIn()
    const server = stubServer({
      validToken: "none",
      refresh: () => json(401, { error: "token_expired", detail: "Refresh token expired" }),
    })

    const error = await pincer.status().catch((e: unknown) => e)
    expect(error).toBeInstanceOf(HTTPError)
    expect((error as HTTPError).response.status).toBe(401)

    expect(server.to("/api/status")).toHaveLength(1) // nothing to retry with
    const state = useAuthStore.getState()
    expect(state.accessToken).toBeNull()
    expect(state.refreshToken).toBeNull()
    expect(state.user).toBeNull()
    expect(state.isConnected).toBe(false)
    expect(redirect).toHaveBeenCalledWith("/login")
  })

  it("no refresh token → session cleared and redirect, no refresh call", async () => {
    signIn()
    useAuthStore.setState({ refreshToken: null })
    const server = stubServer({ validToken: "none" })

    await expect(pincer.status()).rejects.toBeInstanceOf(HTTPError)

    expect(server.to("/api/auth/refresh")).toHaveLength(0)
    expect(useAuthStore.getState().isConnected).toBe(false)
    expect(redirect).toHaveBeenCalledWith("/login")
  })

  it("a still-401 retry is not refreshed or retried again", async () => {
    signIn()
    const server = stubServer({ validToken: "none", refresh: () => json(200, pair(2)) })

    await expect(pincer.status()).rejects.toBeInstanceOf(HTTPError)

    expect(server.to("/api/status")).toHaveLength(2)
    expect(server.to("/api/auth/refresh")).toHaveLength(1)
  })

  it("a 401 from login does not trigger a refresh or touch the session", async () => {
    signIn()
    const server = stubServer({
      validToken: "access-1",
      routes: {
        "/api/auth/login": () => json(401, { error: "invalid_credentials", detail: "Invalid credentials" }),
      },
    })

    const error = await pincer.login(BASE, "alice", "wrong").catch((e: unknown) => e)
    expect((error as HTTPError).response.status).toBe(401)

    expect(server.to("/api/auth/login")).toHaveLength(1)
    expect(server.to("/api/auth/refresh")).toHaveLength(0)
    expect(useAuthStore.getState().accessToken).toBe("access-1")
    expect(redirect).not.toHaveBeenCalled()
  })

  it("429 does not clear the session, refresh, or retry", async () => {
    signIn()
    const locked = () => json(429, { error: "locked_out", detail: "Locked" }, { "Retry-After": "600" })
    const server = stubServer({ validToken: "access-1", routes: { "/api/status": locked } })

    const error = await pincer.status().catch((e: unknown) => e)
    expect((error as HTTPError).response.status).toBe(429)

    expect(server.to("/api/status")).toHaveLength(1)
    expect(server.to("/api/auth/refresh")).toHaveLength(0)
    expect(useAuthStore.getState().accessToken).toBe("access-1")
    expect(useAuthStore.getState().isConnected).toBe(true)
    expect(redirect).not.toHaveBeenCalled()
  })

  it("a locked-out refresh (429) keeps the session", async () => {
    signIn()
    stubServer({
      validToken: "none",
      refresh: () => json(429, { error: "locked_out", detail: "Locked" }, { "Retry-After": "60" }),
    })

    await expect(pincer.status()).rejects.toBeInstanceOf(HTTPError)

    expect(useAuthStore.getState().refreshToken).toBe("refresh-1")
    expect(redirect).not.toHaveBeenCalled()
  })

  it("auth disabled: requests go out without an Authorization header", async () => {
    useAuthStore.getState().setApiUrl(BASE)
    useAuthStore.getState().connectWithoutAuth("1.0")
    const server = stubServer({ validToken: "x", routes: { "/api/status": () => json(200, { ok: true }) } })

    await pincer.status()

    expect(server.to("/api/status")[0].auth).toBeNull()
  })

  it("auth disabled: a 401 neither refreshes nor ends the session", async () => {
    useAuthStore.getState().setApiUrl(BASE)
    useAuthStore.getState().connectWithoutAuth("1.0")
    const redirect = vi.spyOn(authRedirect, "go").mockImplementation(() => {})
    const server = stubServer({
      validToken: "x",
      routes: { "/api/identity/me": () => json(401, { detail: "No authenticated identity" }) },
    })

    await expect(pincer.me()).rejects.toBeInstanceOf(HTTPError)

    expect(server.to("/api/auth/refresh")).toHaveLength(0)
    expect(useAuthStore.getState().isConnected).toBe(true)
    expect(redirect).not.toHaveBeenCalled()
  })

  it("auth disabled, then switched on: the first refusal sends the user to /login", async () => {
    useAuthStore.getState().setApiUrl(BASE)
    useAuthStore.getState().connectWithoutAuth("1.0")
    const redirect = vi.spyOn(authRedirect, "go").mockImplementation(() => {})
    // The agent now requires sign-in: an auth-layer 401 carries an `error` code.
    const server = stubServer({ validToken: "nobody-has-this" })

    await expect(pincer.status()).rejects.toBeInstanceOf(HTTPError)

    expect(server.to("/api/status")).toHaveLength(1) // no retry, no pile of 401s
    expect(server.to("/api/auth/refresh")).toHaveLength(0)
    const state = useAuthStore.getState()
    expect(state.isConnected).toBe(false)
    expect(state.authRequired).toBe(true)
    expect(redirect).toHaveBeenCalledWith("/login")
  })

  it("a rejected refresh does not end a session that was replaced meanwhile", async () => {
    // The password form stores a new pair while a poll's refresh, made with
    // the token that change just invalidated, is still in the air.
    useAuthStore.getState().setApiUrl(BASE)
    useAuthStore.getState().setSession(pair(1))
    const redirect = vi.spyOn(authRedirect, "go").mockImplementation(() => {})
    const server = stubServer({
      validToken: "access-2",
      refresh: () => {
        useAuthStore.getState().setSession(pair(2))
        return json(401, { error: "token_expired", detail: "Token expired" })
      },
    })

    await expect(pincer.status()).resolves.toBeTruthy()

    expect(server.to("/api/status").map((call) => call.auth)).toEqual(["Bearer access-1", "Bearer access-2"])
    expect(useAuthStore.getState().accessToken).toBe("access-2")
    expect(redirect).not.toHaveBeenCalled()
  })

  it("logout tells the server, and never throws when it cannot", async () => {
    useAuthStore.getState().setApiUrl(BASE)
    useAuthStore.getState().setSession(pair(1))
    const server = stubServer({ validToken: "access-1", routes: { "/api/auth/logout": () => new Response(null, { status: 204 }) } })

    await pincer.logout()

    const calls = server.to("/api/auth/logout")
    expect(calls).toHaveLength(1)
    expect(calls[0].method).toBe("POST")
    expect(calls[0].auth).toBe("Bearer access-1")

    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline") }))
    await expect(pincer.logout()).resolves.toBeUndefined()
  })
})

describe("auth store migration", () => {
  it("drops a persisted v0 shared token and signs the user out", async () => {
    localStorage.setItem(
      "pincer-auth",
      JSON.stringify({
        state: { token: "old-shared", apiUrl: "http://old.test", isConnected: true, version: "0.5.0" },
        version: 0,
      }),
    )
    await useAuthStore.persist.rehydrate()
    const state = useAuthStore.getState() as unknown as Record<string, unknown>
    expect(state.token).toBeUndefined()
    expect(state.accessToken).toBeNull()
    expect(state.isConnected).toBe(false)
    expect(state.apiUrl).toBe("http://old.test")
    expect(localStorage.getItem("pincer-auth")).not.toContain("old-shared")
  })
})
