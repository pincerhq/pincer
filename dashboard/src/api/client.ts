import ky from "ky"
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
} from "./types"

function getStoredAuth(): { token?: string; apiUrl?: string } | null {
  const stored = localStorage.getItem("pincer-auth")
  if (!stored) return null
  try {
    const parsed = JSON.parse(stored)
    return parsed.state ?? null
  } catch {
    return null
  }
}

function getBaseUrl(): string {
  const auth = getStoredAuth()
  const url = auth?.apiUrl?.trim()
  if (url) return url.replace(/\/$/, "")
  return window.location.origin
}

function getToken(): string | null {
  const auth = getStoredAuth()
  return auth?.token ?? null
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
        async (_request, _options, response) => {
          if (response.status === 401) {
            const isLoginPage =
              typeof window !== "undefined" &&
              (window.location.pathname === "/login" ||
                window.location.pathname === "/login/")
            localStorage.removeItem("pincer-auth")
            if (!isLoginPage) {
              window.location.href = "/login"
            }
          }
        },
      ],
    },
    timeout: 30000,
    retry: { limit: 2, methods: ["get"] },
  })
}

let _api: ReturnType<typeof ky.create> | null = null

function api() {
  if (!_api) _api = createApiClient()
  return _api
}

export function resetApiClient() {
  _api = null
}

export const pincer = {
  health: () => api().get("api/health").json<HealthResponse>(),
  status: () => api().get("api/status").json<AgentStatus>(),

  /** Validate token by calling a protected endpoint. Use for login. */
  validateToken: (baseUrl: string, token: string) =>
    ky
      .get(`${baseUrl.replace(/\/$/, "")}/api/status`, {
        headers: { Authorization: `Bearer ${token}` },
        timeout: 10000,
      })
      .json<AgentStatus>(),

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
  /** Places a REAL phone call to PINCER_VOICE_CANARY_NUMBER. */
  triggerCanary: () => api().post("api/ops/canary").json<CanaryTriggerResult>(),
}
