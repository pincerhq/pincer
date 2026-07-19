import type { BuiltinToolInfo, ExtensionItem, IntegrationCardInfo } from "@/api/types"
import { cn } from "@/lib/utils"
import { Lock } from "lucide-react"

interface SkillCardProps {
  skill: ExtensionItem
  viewMode?: "grid" | "list"
  onClick?: () => void
}

function isIntegrationCard(item: ExtensionItem): item is IntegrationCardInfo {
  return item.source === "mcp" || item.source === "integration"
}

function isBuiltin(item: ExtensionItem): item is BuiltinToolInfo {
  return item.source === "builtin"
}

function SourceBadge({ item }: { item: ExtensionItem }) {
  if (item.source === "file") {
    return (
      <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-400">
        {item.root}
      </span>
    )
  }
  if (item.source === "builtin") {
    return (
      <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-slate-500/15 text-slate-400">
        CORE
      </span>
    )
  }
  if (item.source === "mcp") {
    return (
      <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400">
        MCP
      </span>
    )
  }
  return (
    <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-violet-500/15 text-violet-400">
      INT
    </span>
  )
}

function StatusDot({ status }: { status: string }) {
  return (
    <span
      className={cn(
        "text-[10px] font-medium uppercase tracking-wider",
        status === "active"
          ? "text-[var(--color-success)]"
          : status === "error"
            ? "text-[var(--color-danger)]"
            : "text-[var(--color-muted)]",
      )}
    >
      {status}
    </span>
  )
}

function ApprovalBadge({ skill }: { skill: ExtensionItem }) {
  if (!isBuiltin(skill) || !skill.approval_required) return null
  return (
    <span
      title="Requires user approval before running"
      className="inline-flex items-center gap-1 text-[10px] text-[var(--color-warning)]"
    >
      <Lock className="h-3 w-3" />
    </span>
  )
}

export function SkillCard({ skill, viewMode = "grid", onClick }: SkillCardProps) {
  if (viewMode === "list") {
    return (
      <div
        className={cn(
          "flex items-center gap-4 px-4 py-3 bg-[var(--color-card)] transition-colors group",
          onClick ? "hover:bg-white/[0.04] cursor-pointer" : "hover:bg-white/[0.02]",
        )}
        onClick={onClick}
      >
        {/* Badge */}
        <div className="shrink-0 w-10 flex justify-center">
          <SourceBadge item={skill} />
        </div>

        {/* Name */}
        <div className="w-44 shrink-0">
          <p className="text-sm font-medium truncate">{skill.name}</p>
          {isIntegrationCard(skill) && skill.version && (
            <p className="text-[11px] text-[var(--color-muted)] font-mono">{skill.version}</p>
          )}
        </div>

        {/* Description */}
        <p className={cn(
          "flex-1 text-xs text-[var(--color-muted)] truncate",
          skill.source === "mcp" && "font-mono",
        )}>
          {skill.description}
        </p>

        {/* Status */}
        <div className="flex items-center gap-3 shrink-0">
          <ApprovalBadge skill={skill} />
          <StatusDot status={skill.status} />
        </div>
      </div>
    )
  }

  // Grid layout
  return (
    <div
      className={cn(
        "rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-4 transition-colors group",
        onClick
          ? "hover:border-[var(--color-accent)]/50 cursor-pointer"
          : "hover:border-[var(--color-border-hover)]",
      )}
      onClick={onClick}
    >
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium truncate">{skill.name}</h3>
          {isIntegrationCard(skill) && skill.version && (
            <p className="text-xs text-[var(--color-muted)] mt-0.5 font-mono">{skill.version}</p>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <SourceBadge item={skill} />
        </div>
      </div>

      <p className={cn(
        "text-xs text-[var(--color-muted)] mt-2 line-clamp-2",
        skill.source === "mcp" && "font-mono break-all",
      )}>
        {skill.description}
      </p>

      <div className="flex items-center justify-between mt-3">
        <StatusDot status={skill.status} />
        <ApprovalBadge skill={skill} />
      </div>
    </div>
  )
}
