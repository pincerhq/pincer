import { useState, useMemo } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { ConversationList } from "@/components/conversations/ConversationList"
import { ConversationView } from "@/components/conversations/ConversationView"
import { useConversations, useConversation } from "@/api/hooks/useConversations"

export function ConversationsPage() {
  const [selected, setSelected] = useState("")
  const [search, setSearch] = useState("")
  const [channelFilter, setChannelFilter] = useState("")

  const params = useMemo(() => {
    const p: Record<string, string> = { limit: "50" }
    if (search) p.search = search
    if (channelFilter) p.channel = channelFilter
    return p
  }, [search, channelFilter])

  const { data, isLoading } = useConversations(params)
  const { data: conversation, isLoading: convLoading } =
    useConversation(selected)

  return (
    <PageContainer title="Conversations">
      <div className="flex gap-0 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden h-[calc(100vh-10rem)]">
        <div className="w-80 border-r border-[var(--color-border)] shrink-0">
          <ConversationList
            conversations={data?.conversations ?? []}
            loading={isLoading}
            selected={selected}
            onSelect={setSelected}
            search={search}
            onSearchChange={setSearch}
            channelFilter={channelFilter}
            onChannelFilterChange={setChannelFilter}
          />
        </div>
        <div className="flex-1 min-w-0">
          <ConversationView
            messages={conversation?.messages ?? []}
            loading={!!selected && convLoading}
          />
        </div>
      </div>
    </PageContainer>
  )
}
