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
  ScanResult,
  Settings,
  DoctorReport,
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

  skills: () => api().get("api/skills").json<SkillsResponse>(),
  skillScan: (path: string) =>
    api().post("api/skills/scan", { json: { path } }).json<ScanResult>(),
  skillInstall: (url: string) =>
    api()
      .post("api/skills/install", { json: { url } })
      .json<{ success: boolean; name: string; version: string }>(),
  skillDelete: (name: string) =>
    api()
      .delete(`api/skills/${name}`)
      .json<{ success: boolean }>(),

  settings: () => api().get("api/settings").json<Settings>(),
  updateSettings: (data: Partial<Settings>) =>
    api().patch("api/settings", { json: data }).json<Settings>(),

  doctor: () => api().get("api/doctor").json<DoctorReport>(),
}
