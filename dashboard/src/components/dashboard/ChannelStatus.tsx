import type { ChannelInfo } from "@/api/types"
import { CHANNEL_COLORS } from "@/lib/constants"
import { cn } from "@/lib/utils"
import { Skeleton } from "@/components/ui/skeleton"

interface ChannelStatusProps {
  channels?: ChannelInfo[] | Record<string, boolean>
  loading?: boolean
}

/** Convert API channels (object or array) to ChannelInfo[]. */
function toChannelItems(
  channels: ChannelInfo[] | Record<string, boolean> | undefined
): ChannelInfo[] {
  if (!channels) return []
  if (Array.isArray(channels)) return channels
  return Object.entries(channels).map(([type, connected]) => ({
    name: type,
    type,
    connected: !!connected,
  }))
}

export function ChannelStatus({ channels, loading }: ChannelStatusProps) {
  if (loading) {
    return (
      <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5">
        <Skeleton className="h-3 w-28 bg-white/[0.06]" />
        <div className="mt-4 space-y-3">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-4 w-full bg-white/[0.06]" />
          ))}
        </div>
      </div>
    )
  }

  const items = toChannelItems(channels)

  return (
    <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5">
      <p className="text-xs font-medium text-[var(--color-muted)] uppercase tracking-wider">
        Channels
      </p>
      {items.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--color-muted)]">
          No channels configured
        </p>
      ) : (
        <div className="mt-4 space-y-3">
          {items.map((ch) => (
            <div key={ch.name} className="flex items-center gap-3">
              <div
                className={cn(
                  "h-2 w-2 rounded-full",
                  ch.connected
                    ? "bg-[var(--color-success)]"
                    : "bg-[var(--color-danger)]",
                )}
              />
              <span className="text-sm text-[var(--color-foreground)] capitalize">
                {ch.type}
              </span>
              <span
                className="ml-auto h-1.5 w-1.5 rounded-full"
                style={{
                  backgroundColor:
                    CHANNEL_COLORS[ch.type] ?? "var(--color-muted)",
                }}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
