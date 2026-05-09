import { AlertCircle, RefreshCw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

interface ApiErrorBannerProps {
  error?: Error | unknown | null
  onRetry?: () => void
  className?: string
}

export function ApiErrorBanner({ error, onRetry, className }: ApiErrorBannerProps) {
  if (!error) return null

  const message =
    error instanceof Error
      ? error.message
      : "Failed to load data from the backend"

  return (
    <div
      role="alert"
      className={cn(
        "flex items-center gap-3 rounded-lg border border-[var(--color-danger)]/30 bg-[var(--color-danger)]/10 px-4 py-3 text-sm",
        className,
      )}
    >
      <AlertCircle className="h-4 w-4 shrink-0 text-[var(--color-danger)]" />
      <span className="flex-1 text-[var(--color-danger)]">{message}</span>
      {onRetry && (
        <Button
          variant="ghost"
          size="sm"
          onClick={onRetry}
          className="h-7 text-xs text-[var(--color-danger)] hover:bg-[var(--color-danger)]/20"
        >
          <RefreshCw className="h-3 w-3 mr-1.5" />
          Retry
        </Button>
      )}
    </div>
  )
}
