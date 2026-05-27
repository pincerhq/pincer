import type { Message } from "@/api/types"
import { formatDateTime } from "@/lib/formatters"
import { cn } from "@/lib/utils"
import { Skeleton } from "@/components/ui/skeleton"
import { Wrench } from "lucide-react"

interface ConversationViewProps {
  messages: Message[]
  loading?: boolean
  createdAt?: string
}

export function ConversationView({ messages, loading, createdAt }: ConversationViewProps) {
  if (loading) {
    return (
      <div className="p-6 space-y-4">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className={cn("flex", i % 2 === 0 ? "justify-end" : "")}>
            <Skeleton className="h-16 w-3/4 bg-white/[0.06] rounded-xl" />
          </div>
        ))}
      </div>
    )
  }

  if (!messages.length) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-[var(--color-muted)]">
        Select a conversation to view messages
      </div>
    )
  }

  return (
    <div className="p-6 space-y-3 overflow-y-auto">
      {createdAt && (
        <p className="text-center text-[10px] text-[var(--color-muted)] font-mono pb-2">
          {formatDateTime(createdAt)}
        </p>
      )}
      {messages.map((msg, i) => {
        if (msg.role === "tool") {
          return (
            <div key={i} className="flex justify-center">
              <div className="inline-flex items-center gap-2 rounded-lg bg-white/[0.03] border border-[var(--color-border)] px-3 py-2 text-xs font-mono text-[var(--color-muted)]">
                <Wrench className="h-3 w-3" />
                <span className="text-[var(--color-accent)]">
                  {msg.tool_name}
                </span>
                <span className="truncate max-w-[300px]">{msg.content}</span>
              </div>
            </div>
          )
        }

        const isUser = msg.role === "user"

        return (
          <div
            key={i}
            className={cn("flex", isUser ? "justify-end" : "justify-start")}
          >
            <div
              className={cn(
                "max-w-[75%] rounded-xl px-4 py-2.5",
                isUser
                  ? "bg-[var(--color-accent)] text-[var(--color-accent-foreground)]"
                  : "bg-white/[0.06] text-[var(--color-foreground)]",
              )}
            >
              <p className="text-sm whitespace-pre-wrap break-words">
                {msg.content}
              </p>
            </div>
          </div>
        )
      })}
    </div>
  )
}
