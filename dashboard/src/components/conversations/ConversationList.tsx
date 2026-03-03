import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ChannelBadge } from "./ChannelBadge"
import type { ConversationPreview } from "@/api/types"
import { formatRelative } from "@/lib/formatters"
import { cn } from "@/lib/utils"

interface ConversationListProps {
  conversations: ConversationPreview[]
  loading?: boolean
  selected?: string
  onSelect: (id: string) => void
  search: string
  onSearchChange: (v: string) => void
  channelFilter: string
  onChannelFilterChange: (v: string) => void
}

export function ConversationList({
  conversations,
  loading,
  selected,
  onSelect,
  search,
  onSearchChange,
  channelFilter,
  onChannelFilterChange,
}: ConversationListProps) {
  return (
    <div className="flex flex-col h-full">
      <div className="p-3 space-y-2 border-b border-[var(--color-border)]">
        <Input
          placeholder="Search conversations..."
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          className="bg-[var(--color-background)] border-[var(--color-border)]"
        />
        <select
          value={channelFilter}
          onChange={(e) => onChannelFilterChange(e.target.value)}
          className="w-full h-8 rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-2 text-xs text-[var(--color-foreground)] focus:outline-none"
        >
          <option value="">All channels</option>
          <option value="telegram">Telegram</option>
          <option value="whatsapp">WhatsApp</option>
          <option value="discord">Discord</option>
          <option value="cli">CLI</option>
          <option value="web">Web</option>
        </select>
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="p-3 space-y-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-16 w-full bg-white/[0.06]" />
            ))}
          </div>
        ) : !conversations.length ? (
          <p className="p-6 text-center text-sm text-[var(--color-muted)]">
            No conversations found
          </p>
        ) : (
          conversations.map((conv) => (
            <button
              key={conv.id}
              onClick={() => onSelect(conv.id)}
              className={cn(
                "w-full text-left px-3 py-3 border-b border-[var(--color-border)] transition-colors",
                selected === conv.id
                  ? "bg-white/[0.06]"
                  : "hover:bg-white/[0.03]",
              )}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium truncate">
                  {conv.user_id}
                </span>
                <ChannelBadge channel={conv.channel} />
              </div>
              <p className="text-xs text-[var(--color-muted)] truncate">
                {conv.last_message}
              </p>
              <div className="flex items-center justify-between mt-1">
                <span className="text-[10px] text-[var(--color-muted)] font-mono">
                  {conv.message_count} messages
                </span>
                <span className="text-[10px] text-[var(--color-muted)] font-mono">
                  {formatRelative(conv.updated_at)}
                </span>
              </div>
            </button>
          ))
        )}
      </div>
    </div>
  )
}
