export interface HealthResponse {
  status: string
  version: string
}

export interface ChannelInfo {
  name: string
  type: string
  connected: boolean
  uptime_seconds?: number
  message_count?: number
}

export interface AgentStatus {
  agent_running: boolean
  channels: ChannelInfo[]
  uptime_seconds: number
  version: string
  active_sessions: number
}

export interface BudgetInfo {
  daily_limit: number
  spent_today: number
  spent_pct: number
  remaining: number
}

export interface CostsToday {
  date: string
  total_usd: number
  by_model: Record<string, number>
  by_tool: Record<string, number>
  request_count: number
  budget: BudgetInfo
}

export interface CostsHistoryEntry {
  date: string
  total_usd: number
  request_count: number
}

export interface CostsHistory {
  period_days: number
  data: CostsHistoryEntry[]
  totals: {
    total_usd: number
    total_requests: number
  }
}

export interface ToolCost {
  tool: string
  total_usd: number
  call_count: number
  avg_cost: number
}

export interface CostsByTool {
  period_days: number
  tools: ToolCost[]
}

export interface ModelCost {
  model: string
  total_usd: number
  request_count: number
  total_tokens: number
}

export interface CostsByModel {
  period_days: number
  models: ModelCost[]
}

export interface AuditEntry {
  id: string
  timestamp: string
  user_id: string
  action: string
  tool?: string
  input_summary?: string
  output_summary?: string
  approved: boolean
  cost_usd?: number
  duration_ms?: number
  metadata?: Record<string, unknown>
}

export interface AuditResponse {
  entries: AuditEntry[]
  total: number
}

export interface AuditStats {
  total_entries: number
  by_action: Record<string, number>
  by_tool: Record<string, number>
  total_cost_usd: number
  failed_actions: number
}

export interface ConversationPreview {
  id: string
  user_id: string
  channel: string
  last_message: string
  message_count: number
  created_at: string
  updated_at: string
}

export interface Message {
  role: "user" | "assistant" | "system" | "tool"
  content: string
  timestamp: string
  tool_name?: string
  tool_input?: Record<string, unknown>
  images?: string[]
}

export interface Conversation {
  id: string
  user_id: string
  channel: string
  messages: Message[]
  created_at: string
  updated_at: string
}

export interface ConversationsResponse {
  conversations: ConversationPreview[]
  total: number
}

export interface SkillInfo {
  name: string
  version: string
  description: string
  author: string
  safety_score: number
  status: "active" | "disabled" | "error"
  permissions: string[]
  tools: string[]
}

export interface SkillsResponse {
  skills: SkillInfo[]
}

export interface ScanIssue {
  severity: "critical" | "warning" | "info"
  message: string
  line?: number
  file?: string
}

export interface ScanResult {
  score: number
  issues: ScanIssue[]
  verdict: "pass" | "warn" | "fail"
}

export interface SettingsLLM {
  provider: string
  model: string
  api_key_set: boolean
  max_tokens: number
  temperature: number
}

export interface SettingsChannels {
  telegram_enabled: boolean
  telegram_token_set: boolean
  whatsapp_enabled: boolean
  discord_enabled: boolean
  discord_token_set: boolean
  web_enabled: boolean
}

export interface SettingsBudget {
  daily_limit: number
  per_conversation_limit: number
  per_tool_limit: number
  auto_downgrade: boolean
}

export interface SettingsSecurity {
  allowed_users: string[]
  require_approval_for: string[]
  audit_enabled: boolean
  rate_limit_messages: number
  rate_limit_tools: number
}

export interface Settings {
  llm: SettingsLLM
  channels: SettingsChannels
  budget: SettingsBudget
  security: SettingsSecurity
  system_prompt: string
  timezone: string
}

export interface DoctorCheck {
  id: string
  name: string
  category: string
  status: "pass" | "warn" | "fail"
  message: string
  fix_hint?: string
}

export interface DoctorReport {
  score: number
  passed: number
  warnings: number
  critical: number
  checks: DoctorCheck[]
}
