import ky, { HTTPError } from "ky"
import { useAuthStore } from "@/stores/auth"
import type {
  HealthResponse,
  AgentStatus,
  CostsToday,
  CostsHistory,
  CostsByTool,
  CostsByModel,
  AuditResponse,
  AuditStats,
  ConversationsResponse,
  Conversation,
  SkillsResponse,
  SkillDetail,
  IntegrationsResponse,
  Settings,
  DoctorReport,
  IntegrationDetail,
  Identity,
  IdentityListResponse,
  ScheduledTasksResponse,
  GoldenSignals,
  OpsAlert,
  SLOReport,
  FailureBreakdown,
  CanaryRun,
  CanaryTriggerResult,
  VoiceCallSummary,
  VoiceActiveCall,
  TelephonyOverview,
  TelephonyCallList,
  TelephonyCallDetail,
  TelephonyEvent,
  TelephonySpan,
  TelephonyTurn,
  TelephonyAlert,
  TelephonyHealth,
  TelephonyMetricDefinition,
  TokenPair,
  Me,
  ApiKeyInfo,
  ApiKeyCreated,
  ListenTicket,
} from "./types"

const LOGIN_PATH = "/login"
const REQUEST_TIMEOUT_MS = 30000
const AUTH_PATHS = ["/api/auth/login", "/api/auth/refresh"]

function trimUrl(url: string): string {
  return url.trim().replace(/\/$/, "")
}

export function getBaseUrl(): string {
  const url = trimUrl(useAuthStore.getState().apiUrl ?? "")
  return url || window.location.origin
}

/** The current access token (null when signed out or auth is disabled). */
export function getToken(): string | null {
  return useAuthStore.getState().accessToken
}

/** Indirection so the hard redirect can be observed in tests. */
export const authRedirect = {
  go: (path: string) => window.location.assign(path),
}

function endSession(): void {
  useAuthStore.getState().logout()
  const path = window.location.pathname
  if (path !== LOGIN_PATH && path !== `${LOGIN_PATH}/`) authRedirect.go(LOGIN_PATH)
}

type RefreshResult = "ok" | "rejected" | "unavailable"

async function requestRefresh(): Promise<RefreshResult> {
  const refreshToken = useAuthStore.getState().refreshToken
  if (!refreshToken) return "rejected"
  try {
    const pair = await ky
      .post(`${getBaseUrl()}/api/auth/refresh`, {
        json: { refresh_token: refreshToken },
        timeout: 10000,
        retry: 0,
      })
      .json<TokenPair>()
    useAuthStore.getState().setSession(pair)
    return "ok"
  } catch (err) {
    // Only an answer about the token itself ends the session. A lockout (429),
    // a 5xx or a dropped connection says nothing about whether it is valid.
    if (err instanceof HTTPError && err.response.status >= 400 && err.response.status < 500) {
      return err.response.status === 429 ? "unavailable" : "rejected"
    }
    return "unavailable"
  }
}

let refreshInFlight: Promise<RefreshResult> | null = null

/**
 * Get an access token to retry with after a 401 on `staleToken`.
 *
 * Single-flight: every caller that arrives while a refresh is running shares
 * that one request — the server issues a new refresh token each time, so two
 * parallel refreshes would race each other. Returns null when there is nothing
 * to retry with; if the refresh token was rejected the session is ended and
 * the browser sent to /login.
 */
export async function refreshAccessToken(staleToken: string | null): Promise<string | null> {
  // Someone else already refreshed (or the password form stored a new pair)
  // while this request was in the air: just use what is there.
  const current = getToken()
  if (current && current !== staleToken) return current
  // An agent without sign-in has no session to refresh or to end; its 401
  // (e.g. "no current identity" on the account routes) is just an answer.
  if (!useAuthStore.getState().authRequired) return null

  if (!refreshInFlight) {
    refreshInFlight = requestRefresh().finally(() => {
      refreshInFlight = null
    })
  }
  const result = await refreshInFlight
  if (result === "ok") return getToken()
  if (result === "rejected") endSession()
  return null
}

function bearerOf(request: Request): string | null {
  const header = request.headers.get("Authorization") ?? ""
  return header.startsWith("Bearer ") ? header.slice(7) : null
}

export function createApiClient() {
  return ky.create({
    prefixUrl: getBaseUrl(),
    hooks: {
      beforeRequest: [
        (request) => {
          const token = getToken()
          if (token) {
            request.headers.set("Authorization", `Bearer ${token}`)
          }
        },
      ],
      afterResponse: [
        async (request, _options, response) => {
          if (response.status !== 401) return
          if (AUTH_PATHS.some((path) => new URL(request.url).pathname.endsWith(path))) return
          const token = await refreshAccessToken(bearerOf(request))
          if (!token) return
          // Retried exactly once, outside this client: the bare `ky` has no
          // hooks, so a second 401 is returned as-is instead of looping.
          const retry = new Request(request)
          retry.headers.set("Authorization", `Bearer ${token}`)
          return ky(retry, {
            retry: 0,
            throwHttpErrors: false,
            timeout: REQUEST_TIMEOUT_MS,
          })
        },
      ],
    },
    timeout: REQUEST_TIMEOUT_MS,
    // No 429 here: it means this IP is locked out, and ky would otherwise sit
    // out the whole Retry-After before answering.
    retry: { limit: 2, methods: ["get"], statusCodes: [408, 413, 500, 502, 503, 504] },
  })
}

let _api: ReturnType<typeof ky.create> | null = null
let _apiBaseUrl: string | null = null

function api() {
  const baseUrl = getBaseUrl()
  if (!_api || _apiBaseUrl !== baseUrl) {
    _api = createApiClient()
    _apiBaseUrl = baseUrl
  }
  return _api
}

export function resetApiClient() {
  _api = null
}

/**
 * Human-readable reason from an error response. Auth errors and route errors
 * carry `detail` as a sentence; FastAPI's validation errors carry an array.
 */
export async function errorDetail(err: unknown, fallback: string): Promise<string> {
  if (!(err instanceof HTTPError)) return fallback
  try {
    const body = (await err.response.clone().json()) as { detail?: unknown }
    if (typeof body.detail === "string" && body.detail) return body.detail
    if (Array.isArray(body.detail)) {
      const messages = body.detail
        .map((item) => (item && typeof item === "object" ? (item as { msg?: unknown }).msg : null))
        .filter((msg): msg is string => typeof msg === "string")
      if (messages.length) return messages.join("; ")
    }
  } catch {
    /* not JSON */
  }
  return fallback
}

/** "Try again in …" wait from a 429's Retry-After (seconds), or null. */
export function retryAfterText(response: Response): string | null {
  const seconds = Number(response.headers.get("Retry-After"))
  if (!Number.isFinite(seconds) || seconds <= 0) return null
  if (seconds < 60) {
    const n = Math.ceil(seconds)
    return `${n} ${n === 1 ? "second" : "seconds"}`
  }
  const n = Math.ceil(seconds / 60)
  return `${n} ${n === 1 ? "minute" : "minutes"}`
}

export const pincer = {
  health: () => api().get("api/health").json<HealthResponse>(),
  status: () => api().get("api/status").json<AgentStatus>(),

  /** Health of an agent that is not (yet) the configured one. Public route. */
  healthAt: (baseUrl: string) =>
    ky.get(`${trimUrl(baseUrl)}/api/health`, { timeout: 10000, retry: 0 }).json<HealthResponse>(),

  // ── Auth & own account ──
  /** Sign in with an identity's name or email. Never goes through the
   *  refreshing client: a 401 here means wrong credentials, nothing else. */
  login: (baseUrl: string, identifier: string, password: string) =>
    ky
      .post(`${trimUrl(baseUrl)}/api/auth/login`, {
        json: { identifier, password },
        timeout: 10000,
        retry: 0,
      })
      .json<TokenPair>(),
  me: () => api().get("api/identity/me").json<Me>(),
  /** Invalidates every token issued so far — store the returned pair. */
  changePassword: (currentPassword: string, newPassword: string) =>
    api()
      .put("api/identity/me/password", {
        json: { current_password: currentPassword, new_password: newPassword },
      })
      .json<TokenPair>(),
  apiKey: () => api().get("api/identity/me/api-key").json<ApiKeyInfo>(),
  /** The full key is in this response only. `force` replaces an existing key. */
  createApiKey: (force = false) =>
    api()
      .post(`api/identity/me/api-key${force ? "?force=true" : ""}`)
      .json<ApiKeyCreated>(),
  listenTicket: (callSid: string) =>
    api()
      .post(`api/voice/listen/${encodeURIComponent(callSid)}/ticket`)
      .json<ListenTicket>(),

  costsToday: () => api().get("api/costs/today").json<CostsToday>(),
  costsHistory: (days = 30) =>
    api().get(`api/costs/history?days=${days}`).json<CostsHistory>(),
  costsByTool: (days = 7) =>
    api().get(`api/costs/by-tool?days=${days}`).json<CostsByTool>(),
  costsByModel: (days = 7) =>
    api().get(`api/costs/by-model?days=${days}`).json<CostsByModel>(),

  audit: (params?: Record<string, string>) => {
    const query = params ? `?${new URLSearchParams(params).toString()}` : ""
    return api().get(`api/audit${query}`).json<AuditResponse>()
  },
  auditStats: () => api().get("api/audit/stats").json<AuditStats>(),

  conversations: (params?: Record<string, string>) => {
    const query = params ? `?${new URLSearchParams(params).toString()}` : ""
    return api().get(`api/conversations${query}`).json<ConversationsResponse>()
  },
  conversation: (id: string) =>
    api().get(`api/conversations/${id}`).json<Conversation>(),

  identities: (params?: Record<string, string>) => {
    const query = params ? `?${new URLSearchParams(params).toString()}` : ""
    return api().get(`api/identity${query}`).json<IdentityListResponse>()
  },
  identity: (id: string) => api().get(`api/identity/${id}`).json<Identity>(),

  schedules: (params?: Record<string, string>) => {
    const query = params ? `?${new URLSearchParams(params).toString()}` : ""
    return api().get(`api/schedules${query}`).json<ScheduledTasksResponse>()
  },

  skills: () => api().get("api/skills").json<SkillsResponse>(),
  skill: (name: string) => api().get(`api/skills/${encodeURIComponent(name)}`).json<SkillDetail>(),

  settings: () => api().get("api/settings").json<Settings>(),
  updateSettings: (data: Partial<Settings>) =>
    api().patch("api/settings", { json: data }).json<Settings>(),

  doctor: () => api().get("api/doctor").json<DoctorReport>(),

  integrations: () => api().get("api/integrations").json<IntegrationsResponse>(),
  integration: (slug: string) =>
    api().get(`api/integrations/${slug}`).json<IntegrationDetail>(),

  // ── Voice Ops (Sprint 9) ──
  opsSignals: () => api().get("api/ops/signals").json<GoldenSignals>(),
  opsAlerts: () => api().get("api/ops/alerts").json<OpsAlert[]>(),
  opsSlo: () => api().get("api/ops/slo").json<SLOReport>(),
  opsFailures: (hours = 168) =>
    api().get(`api/ops/failures?hours=${hours}`).json<FailureBreakdown>(),
  opsCanary: (limit = 20) =>
    api().get(`api/ops/canary?limit=${limit}`).json<CanaryRun[]>(),
  voiceCalls: (limit = 50) =>
    api().get(`api/voice/calls?limit=${limit}`).json<VoiceCallSummary[]>(),
  voiceActive: () => api().get("api/voice/active").json<VoiceActiveCall[]>(),
  /** Places a REAL phone call to PINCER_VOICE_CANARY_NUMBER. */
  triggerCanary: () => api().post("api/ops/canary").json<CanaryTriggerResult>(),

  // ── Telephony telemetry ──
  telephonyOverview: (params: Record<string, string>) =>
    api().get(`api/telephony/overview?${new URLSearchParams(params)}`).json<TelephonyOverview>(),
  telephonyCalls: (params: Record<string, string>) =>
    api().get(`api/telephony/calls?${new URLSearchParams(params)}`).json<TelephonyCallList>(),
  telephonyCall: (ref: string) =>
    api().get(`api/telephony/calls/${encodeURIComponent(ref)}`).json<TelephonyCallDetail>(),
  telephonyCallEvents: (ref: string) =>
    api().get(`api/telephony/calls/${encodeURIComponent(ref)}/events`).json<TelephonyEvent[]>(),
  telephonyCallSpans: (ref: string) =>
    api().get(`api/telephony/calls/${encodeURIComponent(ref)}/spans`).json<TelephonySpan[]>(),
  telephonySlowestTurns: (params: Record<string, string>) =>
    api().get(`api/telephony/turns/slowest?${new URLSearchParams(params)}`).json<TelephonyTurn[]>(),
  telephonyAlerts: () => api().get("api/telephony/alerts").json<TelephonyAlert[]>(),
  telephonyHealth: () => api().get("api/telephony/health").json<TelephonyHealth>(),
  telephonyMetricDefinitions: () =>
    api().get("api/telephony/metrics").json<TelephonyMetricDefinition[]>(),
}
