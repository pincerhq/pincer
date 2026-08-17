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

export interface Message {
  role: "user" | "assistant" | "system" | "tool"
  content: string
  timestamp?: string
  tool_name?: string
  tool_input?: Record<string, unknown>
  images?: string[]
}

export interface ConversationPreview {
  id: string
  user_id: string
  preview: string
  tags: string[]
  messages: Message[]
  created_at: string
}

export interface Conversation {
  id: string
  user_id: string
  category: string
  tags: string[]
  messages: Message[]
  created_at: string
}

export interface ConversationsResponse {
  conversations: ConversationPreview[]
  total: number
  limit: number
  offset: number
}

export interface SkillInfo {
  name: string
  description: string
  status: "active" | "disabled" | "error"
  source: "file"
  /** SKILL.md discovery root. */
  root: "bundled" | "user"
  /** Directory name on disk — GET /api/skills/{name} matches on this, not `name`. */
  dir: string
}

export interface BuiltinToolInfo {
  name: string
  description: string
  status: "active" | "disabled" | "error"
  source: "builtin"
  approval_required: boolean
}

export interface IntegrationCardInfo {
  name: string
  description: string
  status: "active" | "disabled" | "error"
  source: "mcp" | "integration"
  slug?: string
  version?: string
  author?: string
  tools?: string[]
}

export type IntegrationInfo = IntegrationCardInfo | BuiltinToolInfo

export interface IntegrationTool {
  name: string
  description: string
}

export interface IntegrationCategory {
  name: string
  tools: IntegrationTool[]
}

export interface IntegrationDetail {
  slug: string
  name: string
  author: string
  description: string
  status: "active" | "disabled"
  tool_count: number
  categories: IntegrationCategory[]
  usage: {
    total_calls: number
    by_tool: Record<string, number>
  }
}

export interface SkillDetail extends SkillInfo {
  /** Full markdown body of SKILL.md (frontmatter stripped). */
  body: string
  /** Relative paths of other files in the skill's directory. */
  files: string[]
}

export interface SkillsResponse {
  skills: SkillInfo[]
}

export interface IntegrationsResponse {
  integrations: IntegrationInfo[]
}

/** Shared shape rendered by SkillCard/SkillGrid — either a SKILL.md skill or an integration/MCP entry. */
export type ExtensionItem = SkillInfo | IntegrationInfo

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

export interface IdentityChannel {
  channel: string
  channel_user_id: string
  linked_at: string
}

export interface Identity {
  pincer_user_id: string
  preferred_channel: string
  display_name: string
  created_at: string
  channels: IdentityChannel[]
}

export interface IdentityListResponse {
  identities: Identity[]
  total: number
}

export interface ScheduledTaskAction {
  type: "briefing" | "custom" | string
  prompt?: string
  allowed_tools?: string[]
}

export interface ScheduledTask {
  id: number
  pincer_user_id: string
  name: string
  kind: "recurring" | "one_time"
  timing: "future" | "past"
  cron_expr: string
  timezone: string
  channel: string
  enabled: boolean
  action: ScheduledTaskAction
  last_run_at: string | null
  next_run_at: string | null
  created_at: string
  updated_at: string
}

export interface ScheduledTasksResponse {
  tasks: ScheduledTask[]
  total: number
  future_count: number
  past_count: number
}
