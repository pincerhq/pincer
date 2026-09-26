import { format, formatDistanceToNow, parseISO } from "date-fns"

export function formatCurrency(amount: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(amount)
}

export function formatCompactCurrency(amount: number): string {
  return `$${amount.toFixed(2)}`
}

export function formatNumber(n: number): string {
  return new Intl.NumberFormat("en-US").format(n)
}

export function formatCompactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return n.toString()
}

export function formatDate(date: string | Date): string {
  const d = typeof date === "string" ? parseISO(date) : date
  return format(d, "MMM d, yyyy")
}

export function formatDateTime(date: string | Date): string {
  const d = typeof date === "string" ? parseISO(date) : date
  return format(d, "MMM d, yyyy HH:mm:ss")
}

export function formatRelative(date: string | Date): string {
  const d = typeof date === "string" ? parseISO(date) : date
  return formatDistanceToNow(d, { addSuffix: true })
}

export function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(2)}M`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`
  return tokens.toString()
}

export function formatPercent(value: number): string {
  return `${value.toFixed(0)}%`
}

/** A short, still-distinguishing form of a row id.
 *
 * Ids are UUIDv7, whose leading 12 hex digits are a millisecond timestamp:
 * two calls minted in the same millisecond share all twelve, two in the same
 * second share roughly the first nine, and everything in a given day shares
 * the first five. Truncating from the front therefore stops
 * identifying anything — which matters precisely when there is no provider
 * CallSid to show instead. The tail is the counter and the random bits.
 */
export function shortId(id: string): string {
  return id.length <= 12 ? id : id.slice(-12)
}
