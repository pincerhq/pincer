import { CHANNEL_COLORS } from "@/lib/constants"

interface ChannelBadgeProps {
  channel: string
}

export function ChannelBadge({ channel }: ChannelBadgeProps) {
  const color = CHANNEL_COLORS[channel] ?? "#888"

  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-1.5 py-px text-[9px] font-medium uppercase tracking-wider"
      style={{
        backgroundColor: `${color}15`,
        color,
        border: `1px solid ${color}30`,
      }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: color }}
      />
      {channel}
    </span>
  )
}
