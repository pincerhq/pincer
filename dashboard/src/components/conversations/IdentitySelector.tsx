import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import type { Identity } from "@/api/types"

interface IdentitySelectorProps {
  identities: Identity[]
  selected: string
  onSelect: (id: string) => void
  loading: boolean
  error: boolean
  onRetry: () => void
}

export function IdentitySelector({
  identities,
  selected,
  onSelect,
  loading,
  error,
  onRetry,
}: IdentitySelectorProps) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 px-1" data-testid="identity-skeleton">
        <Skeleton className="h-7 w-20 rounded-full" />
        <Skeleton className="h-7 w-20 rounded-full" />
        <Skeleton className="h-7 w-20 rounded-full" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center gap-2 px-1 text-sm text-[var(--color-muted)]">
        <span>Failed to load identities.</span>
        <button
          onClick={onRetry}
          className="text-[var(--color-foreground)] underline underline-offset-2 hover:opacity-70 transition-opacity"
        >
          Retry
        </button>
      </div>
    )
  }

  const isSingle = identities.length === 1

  return (
    <div className="flex items-center gap-2 px-1 flex-wrap">
      {identities.map((identity) => {
        const label = identity.display_name || identity.pincer_user_id
        const isActive = identity.pincer_user_id === selected

        if (isSingle) {
          return (
            <span
              key={identity.pincer_user_id}
              className={cn(
                "inline-flex items-center px-3 py-1 rounded-full text-xs font-medium border border-transparent",
                "bg-[var(--color-accent)] text-[var(--color-accent-foreground)]",
                "cursor-default select-none",
              )}
            >
              {label}
            </span>
          )
        }

        return (
          <button
            key={identity.pincer_user_id}
            role="button"
            aria-pressed={isActive}
            data-interactive="true"
            onClick={() => onSelect(identity.pincer_user_id)}
            className={cn(
              "inline-flex items-center px-3 py-1 rounded-full text-xs font-medium border transition-colors",
              isActive
                ? "border-transparent bg-[var(--color-accent)] text-[var(--color-accent-foreground)]"
                : "border-[var(--color-border)] bg-transparent text-[var(--color-muted)] hover:text-[var(--color-foreground)] hover:border-[var(--color-foreground)]/50",
            )}
          >
            {label}
          </button>
        )
      })}
    </div>
  )
}
