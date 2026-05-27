import { useState, useEffect } from "react"
import { useSearchParams } from "react-router-dom"
import { PageContainer } from "@/components/layout/PageContainer"
import { ConversationList } from "@/components/conversations/ConversationList"
import { ConversationView } from "@/components/conversations/ConversationView"
import { IdentitySelector } from "@/components/conversations/IdentitySelector"
import { useIdentities } from "@/api/hooks/useIdentities"
import { useConversation } from "@/api/hooks/useConversations"

export function ConversationsPage() {
  const [selected, setSelected] = useState("")
  const [searchParams, setSearchParams] = useSearchParams()
  const identityParam = searchParams.get("identity") ?? ""

  const {
    data: identitiesData,
    isLoading: identitiesLoading,
    isError: identitiesError,
    refetch: retryIdentities,
  } = useIdentities()

  useEffect(() => {
    if (!identityParam && identitiesData?.identities.length) {
      setSearchParams(
        { identity: identitiesData.identities[0].pincer_user_id },
        { replace: true },
      )
    }
  }, [identitiesData, identityParam, setSearchParams])

  const { data: conversation, isLoading: convLoading } = useConversation(selected)

  const currentIdentity = identitiesData?.identities.find(
    (i) => i.pincer_user_id === identityParam,
  )

  return (
    <PageContainer title="Conversations">
      <div className="mb-3">
        <IdentitySelector
          identities={identitiesData?.identities ?? []}
          selected={identityParam}
          onSelect={(id) => {
            setSelected("")
            setSearchParams({ identity: id })
          }}
          loading={identitiesLoading}
          error={identitiesError}
          onRetry={() => void retryIdentities()}
        />
      </div>

      <div className="flex gap-0 rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden h-[calc(100vh-13rem)]">
        <div className="w-80 border-r border-[var(--color-border)] shrink-0">
          <ConversationList
            userId={identityParam}
            channels={currentIdentity?.channels ?? []}
            selected={selected}
            onSelect={setSelected}
          />
        </div>
        <div className="flex-1 min-w-0">
          <ConversationView
            messages={conversation?.messages ?? []}
            loading={!!selected && convLoading}
            createdAt={conversation?.created_at}
          />
        </div>
      </div>
    </PageContainer>
  )
}
