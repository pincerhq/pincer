import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, it, expect, vi } from "vitest"
import { IdentitySelector } from "./IdentitySelector"
import type { Identity } from "@/api/types"

const makeIdentity = (id: string, displayName?: string): Identity => ({
  pincer_user_id: id,
  preferred_channel: "telegram",
  display_name: displayName ?? "",
  created_at: "2024-01-01T00:00:00",
  channels: [],
})

const noop = () => {}

describe("IdentitySelector", () => {
  it("shows skeleton chips while loading", () => {
    render(
      <IdentitySelector
        identities={[]}
        selected=""
        onSelect={noop}
        loading={true}
        error={false}
        onRetry={noop}
      />,
    )
    expect(screen.getByTestId("identity-skeleton")).toBeInTheDocument()
  })

  it("shows error message and retry button on error", () => {
    const retry = vi.fn()
    render(
      <IdentitySelector
        identities={[]}
        selected=""
        onSelect={noop}
        loading={false}
        error={true}
        onRetry={retry}
      />,
    )
    expect(screen.getByText(/failed to load identities/i)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument()
  })

  it("calls onRetry when retry button is clicked", async () => {
    const retry = vi.fn()
    render(
      <IdentitySelector
        identities={[]}
        selected=""
        onSelect={noop}
        loading={false}
        error={true}
        onRetry={retry}
      />,
    )
    await userEvent.click(screen.getByRole("button", { name: /retry/i }))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it("renders non-interactive chip for single identity", () => {
    const onSelect = vi.fn()
    render(
      <IdentitySelector
        identities={[makeIdentity("john", "John Doe")]}
        selected="john"
        onSelect={onSelect}
        loading={false}
        error={false}
        onRetry={noop}
      />,
    )
    const chip = screen.getByText("John Doe")
    expect(chip.closest("[data-interactive]")).toBeNull()
    // clicking the static chip should not call onSelect
  })

  it("shows display_name on chip when available", () => {
    render(
      <IdentitySelector
        identities={[makeIdentity("alice", "Alice Smith"), makeIdentity("bob", "Bob Jones")]}
        selected="alice"
        onSelect={noop}
        loading={false}
        error={false}
        onRetry={noop}
      />,
    )
    expect(screen.getByText("Alice Smith")).toBeInTheDocument()
    expect(screen.getByText("Bob Jones")).toBeInTheDocument()
  })

  it("falls back to pincer_user_id when display_name is empty", () => {
    render(
      <IdentitySelector
        identities={[makeIdentity("usr_abc123")]}
        selected="usr_abc123"
        onSelect={noop}
        loading={false}
        error={false}
        onRetry={noop}
      />,
    )
    expect(screen.getByText("usr_abc123")).toBeInTheDocument()
  })

  it("calls onSelect with identity id when a chip is clicked (multiple identities)", async () => {
    const onSelect = vi.fn()
    render(
      <IdentitySelector
        identities={[makeIdentity("alice", "Alice"), makeIdentity("bob", "Bob")]}
        selected="alice"
        onSelect={onSelect}
        loading={false}
        error={false}
        onRetry={noop}
      />,
    )
    await userEvent.click(screen.getByRole("button", { name: "Bob" }))
    expect(onSelect).toHaveBeenCalledWith("bob")
  })

  it("marks active chip as selected", () => {
    render(
      <IdentitySelector
        identities={[makeIdentity("alice", "Alice"), makeIdentity("bob", "Bob")]}
        selected="alice"
        onSelect={noop}
        loading={false}
        error={false}
        onRetry={noop}
      />,
    )
    expect(screen.getByRole("button", { name: "Alice" })).toHaveAttribute("aria-pressed", "true")
    expect(screen.getByRole("button", { name: "Bob" })).toHaveAttribute("aria-pressed", "false")
  })
})
