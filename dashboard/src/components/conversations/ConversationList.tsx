import { useState, useMemo, useEffect, useRef } from "react"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ChannelBadge } from "./ChannelBadge"
import { useConversations } from "@/api/hooks/useConversations"
import { formatRelative } from "@/lib/formatters"
import { cn } from "@/lib/utils"
import type { IdentityChannel } from "@/api/types"

interface ConversationListProps {
  userId: string
  channels: IdentityChannel[]
  selected?: string
  onSelect: (id: string) => void
}

export function ConversationList({ userId, channels, selected, onSelect }: ConversationListProps) {
  const [search, setSearch] = useState("")
  const [channelFilter, setChannelFilter] = useState("")

  useEffect(() => {
    setSearch("")
    setChannelFilter("")
  }, [userId])

  const channelUserId = useMemo(
    () => channels.find((c) => c.channel === channelFilter)?.channel_user_id ?? "",
    [channelFilter, channels],
  )

  const params = useMemo(() => {
    if (!userId) return undefined
    const p: Record<string, string> = { limit: "20", user_id: userId }
    if (channelFilter && channelUserId) {
      p.channel = channelFilter
      p.channel_user_id = channelUserId
    }
    return p
  }, [userId, channelFilter, channelUserId])

  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage } =
    useConversations(params)

  const conversations = useMemo(() => {
    let list = data?.pages.flatMap((p) => p.conversations) ?? []
    if (search) {
      const lower = search.toLowerCase()
      list = list.filter(
        (c) =>
          c.user_id.toLowerCase().includes(lower) ||
          c.preview?.toLowerCase().includes(lower),
      )
    }
    return list
  }, [data, search])

  const sentinelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const el = sentinelRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && hasNextPage && !isFetchingNextPage) {
          void fetchNextPage()
        }
      },
      { threshold: 0.1 },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [hasNextPage, isFetchingNextPage, fetchNextPage])

  return (
    <div className="flex flex-col h-full">
      <div className="p-3 space-y-2 border-b border-[var(--color-border)]">
        <Input
          placeholder="Search conversations..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="bg-[var(--color-background)] border-[var(--color-border)]"
        />
        <select
          value={channelFilter}
          onChange={(e) => setChannelFilter(e.target.value)}
          className="w-full h-8 rounded-md border border-[var(--color-border)] bg-[var(--color-background)] px-2 text-xs text-[var(--color-foreground)] focus:outline-none"
        >
          <option value="">All channels</option>
          {channels.map((c) => (
            <option key={c.channel} value={c.channel}>
              {c.channel.charAt(0).toUpperCase() + c.channel.slice(1)}
            </option>
          ))}
        </select>
      </div>

      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
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
          <>
            {conversations.map((conv) => {
              // Channel tags have format user:{channel}:{channel_user_id} (3 parts).
              // Identity tags are user:{id} (2 parts) — skip those.
              const parts = conv.tags
                .map((t) => t.split(":"))
                .find((p) => p[0] === "user" && p.length === 3)
              const channel = parts?.[1] ?? ""
              return (
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
                    <span className="text-sm font-medium truncate">{conv.user_id}</span>
                    <span className="text-[10px] text-[var(--color-muted)] font-mono shrink-0 ml-2">
                      {formatRelative(conv.created_at)}
                    </span>
                  </div>
                  <p className="text-xs text-[var(--color-muted)] truncate mb-1.5">{conv.preview}</p>
                  <div className="flex items-center justify-between">
                    {channel
                      ? <ChannelBadge channel={channel} />
                      : <span />}
                    <span className="text-[10px] text-[var(--color-muted)] font-mono">
                      {conv.messages.length} msg
                    </span>
                  </div>
                </button>
              )
            })}
            <div ref={sentinelRef} className="h-4" />
            {isFetchingNextPage && (
              <div className="p-3">
                <Skeleton className="h-16 w-full bg-white/[0.06]" />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
