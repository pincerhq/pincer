import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { ConversationList } from "./ConversationList"
import type { ConversationsResponse, IdentityChannel } from "@/api/types"

vi.mock("@/api/hooks/useConversations", () => ({
  useConversations: vi.fn(),
}))

import { useConversations } from "@/api/hooks/useConversations"

const mockChannels: IdentityChannel[] = [
  { channel: "telegram", channel_user_id: "12345", linked_at: "" },
  { channel: "whatsapp", channel_user_id: "491234567890", linked_at: "" },
  { channel: "discord", channel_user_id: "987654321", linked_at: "" },
]

const allConversations: ConversationsResponse["conversations"] = [
  {
    id: "1",
    user_id: "john",
    preview: "Hello from telegram",
    tags: ["user:john", "category:exchange", "user:telegram:12345"],
    messages: [],
    created_at: "2024-01-01T10:00:00+00:00",
  },
  {
    id: "2",
    user_id: "john",
    preview: "Hello from whatsapp",
    tags: ["user:john", "category:exchange", "user:whatsapp:491234567890"],
    messages: [],
    created_at: "2024-01-02T10:00:00+00:00",
  },
  {
    id: "3",
    user_id: "john",
    preview: "Hello from discord",
    tags: ["user:john", "category:exchange", "user:discord:987654321"],
    messages: [],
    created_at: "2024-01-03T10:00:00+00:00",
  },
]

function makeMockReturn(conversations: ConversationsResponse["conversations"]) {
  return {
    data: {
      pages: [{ conversations, total: conversations.length, limit: 20, offset: 0 }],
      pageParams: [0],
    },
    isLoading: false,
    isFetchingNextPage: false,
    hasNextPage: false,
    fetchNextPage: vi.fn(),
  } as unknown as ReturnType<typeof useConversations>
}

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

beforeEach(() => {
  vi.mocked(useConversations).mockReturnValue(makeMockReturn(allConversations))
})

describe("ConversationList channel filter", () => {
  it("shows all conversations when no channel filter is selected", () => {
    render(<ConversationList userId="john" channels={mockChannels} onSelect={() => {}} />, {
      wrapper: wrapper(),
    })
    expect(screen.getByText("Hello from telegram")).toBeInTheDocument()
    expect(screen.getByText("Hello from whatsapp")).toBeInTheDocument()
    expect(screen.getByText("Hello from discord")).toBeInTheDocument()
  })

  it("passes channel and channel_user_id to the hook when telegram is selected", async () => {
    render(<ConversationList userId="john" channels={mockChannels} onSelect={() => {}} />, {
      wrapper: wrapper(),
    })
    await userEvent.selectOptions(screen.getByRole("combobox"), "telegram")
    expect(vi.mocked(useConversations)).toHaveBeenLastCalledWith(
      expect.objectContaining({ channel: "telegram", channel_user_id: "12345" }),
    )
  })

  it("passes channel and channel_user_id to the hook when whatsapp is selected", async () => {
    render(<ConversationList userId="john" channels={mockChannels} onSelect={() => {}} />, {
      wrapper: wrapper(),
    })
    await userEvent.selectOptions(screen.getByRole("combobox"), "whatsapp")
    expect(vi.mocked(useConversations)).toHaveBeenLastCalledWith(
      expect.objectContaining({ channel: "whatsapp", channel_user_id: "491234567890" }),
    )
  })

  it("omits channel params when filter is cleared", async () => {
    render(<ConversationList userId="john" channels={mockChannels} onSelect={() => {}} />, {
      wrapper: wrapper(),
    })
    await userEvent.selectOptions(screen.getByRole("combobox"), "telegram")
    await userEvent.selectOptions(screen.getByRole("combobox"), "")
    expect(vi.mocked(useConversations)).toHaveBeenLastCalledWith(
      expect.not.objectContaining({ channel: expect.anything() }),
    )
  })

  it("shows only 'All channels' when identity has no linked channels", () => {
    render(
      <ConversationList userId="john" channels={[]} onSelect={() => {}} />,
      { wrapper: wrapper() },
    )
    const options = screen.getAllByRole("option")
    expect(options).toHaveLength(1)
    expect(options[0]).toHaveTextContent("All channels")
  })
})
