import { useForm } from "react-hook-form"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import type { Settings } from "@/api/types"
import { Loader2, Save } from "lucide-react"

interface SettingsFormValues {
  llm_provider: string
  llm_model: string
  max_tokens: number
  temperature: number
  telegram_enabled: boolean
  whatsapp_enabled: boolean
  discord_enabled: boolean
  web_enabled: boolean
  daily_limit: number
  per_conversation_limit: number
  per_tool_limit: number
  auto_downgrade: boolean
  allowed_users: string
  require_approval_for: string
  audit_enabled: boolean
  rate_limit_messages: number
  rate_limit_tools: number
  system_prompt: string
  timezone: string
}

interface SettingsFormProps {
  settings: Settings
  onSave: (data: Partial<Settings>) => void
  saving?: boolean
}

function settingsToForm(s: Settings): SettingsFormValues {
  return {
    llm_provider: s.llm.provider,
    llm_model: s.llm.model,
    max_tokens: s.llm.max_tokens,
    temperature: s.llm.temperature,
    telegram_enabled: s.channels.telegram_enabled,
    whatsapp_enabled: s.channels.whatsapp_enabled,
    discord_enabled: s.channels.discord_enabled,
    web_enabled: s.channels.web_enabled,
    daily_limit: s.budget.daily_limit,
    per_conversation_limit: s.budget.per_conversation_limit,
    per_tool_limit: s.budget.per_tool_limit,
    auto_downgrade: s.budget.auto_downgrade,
    allowed_users: s.security.allowed_users.join(", "),
    require_approval_for: s.security.require_approval_for.join(", "),
    audit_enabled: s.security.audit_enabled,
    rate_limit_messages: s.security.rate_limit_messages,
    rate_limit_tools: s.security.rate_limit_tools,
    system_prompt: s.system_prompt,
    timezone: s.timezone,
  }
}

function formToSettings(f: SettingsFormValues): Partial<Settings> {
  return {
    llm: {
      provider: f.llm_provider,
      model: f.llm_model,
      api_key_set: true,
      max_tokens: f.max_tokens,
      temperature: f.temperature,
    },
    channels: {
      telegram_enabled: f.telegram_enabled,
      telegram_token_set: true,
      whatsapp_enabled: f.whatsapp_enabled,
      discord_enabled: f.discord_enabled,
      discord_token_set: true,
      web_enabled: f.web_enabled,
    },
    budget: {
      daily_limit: f.daily_limit,
      per_conversation_limit: f.per_conversation_limit,
      per_tool_limit: f.per_tool_limit,
      auto_downgrade: f.auto_downgrade,
    },
    security: {
      allowed_users: f.allowed_users
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      require_approval_for: f.require_approval_for
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
      audit_enabled: f.audit_enabled,
      rate_limit_messages: f.rate_limit_messages,
      rate_limit_tools: f.rate_limit_tools,
    },
    system_prompt: f.system_prompt,
    timezone: f.timezone,
  }
}

function Field({
  label,
  error,
  children,
}: {
  label: string
  error?: string
  children: React.ReactNode
}) {
  return (
    <div>
      <label className="text-xs text-[var(--color-muted)] uppercase tracking-wider">
        {label}
      </label>
      <div className="mt-1.5">{children}</div>
      {error && <p className="text-xs text-[var(--color-danger)] mt-1">{error}</p>}
    </div>
  )
}

function Toggle({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="flex items-center gap-3 cursor-pointer py-1">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative w-9 h-5 rounded-full transition-colors ${checked ? "bg-[var(--color-accent)]" : "bg-white/10"}`}
      >
        <span
          className={`absolute top-0.5 left-0.5 h-4 w-4 rounded-full bg-white transition-transform ${checked ? "translate-x-4" : ""}`}
        />
      </button>
      <span className="text-sm">{label}</span>
    </label>
  )
}

export function SettingsForm({ settings, onSave, saving }: SettingsFormProps) {
  const {
    register,
    handleSubmit,
    watch,
    setValue,
  } = useForm<SettingsFormValues>({
    defaultValues: settingsToForm(settings),
  })

  const onSubmit = (data: SettingsFormValues) => {
    onSave(formToSettings(data))
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} className="space-y-8">
      {/* LLM Section */}
      <section>
        <h3 className="text-sm font-medium mb-4">LLM Provider</h3>
        <div className="grid grid-cols-2 gap-4">
          <Field label="Provider">
            <select
              {...register("llm_provider")}
              className="w-full h-9 rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-3 text-sm"
            >
              <option value="anthropic">Anthropic</option>
              <option value="openai">OpenAI</option>
              <option value="ollama">Ollama</option>
            </select>
          </Field>
          <Field label="Model">
            <Input
              {...register("llm_model")}
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
          <Field label="Max Tokens">
            <Input
              type="number"
              {...register("max_tokens")}
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
          <Field label="Temperature">
            <Input
              type="number"
              step="0.1"
              {...register("temperature")}
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
        </div>
      </section>

      <Separator className="bg-[var(--color-border)]" />

      {/* Channels Section */}
      <section>
        <h3 className="text-sm font-medium mb-4">Channels</h3>
        <div className="grid grid-cols-2 gap-x-8 gap-y-1">
          <Toggle
            label="Telegram"
            checked={watch("telegram_enabled")}
            onChange={(v) => setValue("telegram_enabled", v)}
          />
          <Toggle
            label="WhatsApp"
            checked={watch("whatsapp_enabled")}
            onChange={(v) => setValue("whatsapp_enabled", v)}
          />
          <Toggle
            label="Discord"
            checked={watch("discord_enabled")}
            onChange={(v) => setValue("discord_enabled", v)}
          />
          <Toggle
            label="Web Dashboard"
            checked={watch("web_enabled")}
            onChange={(v) => setValue("web_enabled", v)}
          />
        </div>
      </section>

      <Separator className="bg-[var(--color-border)]" />

      {/* Budget Section */}
      <section>
        <h3 className="text-sm font-medium mb-4">Budget</h3>
        <div className="grid grid-cols-3 gap-4">
          <Field label="Daily Limit ($)">
            <Input
              type="number"
              step="0.5"
              {...register("daily_limit")}
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
          <Field label="Per Conversation ($)">
            <Input
              type="number"
              step="0.1"
              {...register("per_conversation_limit")}
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
          <Field label="Per Tool Call ($)">
            <Input
              type="number"
              step="0.1"
              {...register("per_tool_limit")}
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
        </div>
        <div className="mt-3">
          <Toggle
            label="Auto-downgrade model when budget is tight"
            checked={watch("auto_downgrade")}
            onChange={(v) => setValue("auto_downgrade", v)}
          />
        </div>
      </section>

      <Separator className="bg-[var(--color-border)]" />

      {/* Security Section */}
      <section>
        <h3 className="text-sm font-medium mb-4">Security</h3>
        <div className="space-y-4">
          <Field label="Allowed Users (comma-separated)">
            <Input
              {...register("allowed_users")}
              placeholder="user1, user2"
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
          <Field label="Require Approval For (tools, comma-separated)">
            <Input
              {...register("require_approval_for")}
              placeholder="shell, file_write"
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Messages Rate Limit (per min)">
              <Input
                type="number"
                {...register("rate_limit_messages")}
                className="bg-[var(--color-background)] border-[var(--color-border)]"
              />
            </Field>
            <Field label="Tools Rate Limit (per min)">
              <Input
                type="number"
                {...register("rate_limit_tools")}
                className="bg-[var(--color-background)] border-[var(--color-border)]"
              />
            </Field>
          </div>
          <Toggle
            label="Enable audit logging"
            checked={watch("audit_enabled")}
            onChange={(v) => setValue("audit_enabled", v)}
          />
        </div>
      </section>

      <Separator className="bg-[var(--color-border)]" />

      {/* Advanced Section */}
      <section>
        <h3 className="text-sm font-medium mb-4">Advanced</h3>
        <div className="space-y-4">
          <Field label="System Prompt">
            <textarea
              {...register("system_prompt")}
              rows={4}
              className="w-full rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-3 py-2 text-sm font-mono resize-y focus:outline-none focus:border-[var(--color-accent)]"
            />
          </Field>
          <Field label="Timezone">
            <Input
              {...register("timezone")}
              placeholder="America/New_York"
              className="bg-[var(--color-background)] border-[var(--color-border)]"
            />
          </Field>
        </div>
      </section>

      <div className="flex justify-end">
        <Button
          type="submit"
          disabled={saving}
          className="bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90"
        >
          {saving ? (
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          ) : (
            <Save className="h-4 w-4 mr-2" />
          )}
          Save Changes
        </Button>
      </div>
    </form>
  )
}
