import { Info } from "lucide-react"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { cn } from "@/lib/utils"

/**
 * The "i" that carries the explanation.
 *
 * A latency number is meaningless without its boundaries, and a rate is
 * meaningless without its denominator — but printing all of that next to every
 * figure buries the figures. So the explanation lives one click away and the
 * surface stays readable. It is never *removed*, only folded.
 */
export function InfoHint({
  title,
  children,
  className,
  label = "What this measures",
}: {
  title?: string
  children: React.ReactNode
  className?: string
  label?: string
}) {
  return (
    <Popover>
      <PopoverTrigger
        aria-label={title ? `${label}: ${title}` : label}
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full align-middle",
          "text-[var(--color-muted)] transition-colors hover:bg-white/[0.08] hover:text-[var(--color-foreground)]",
          "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--color-accent)]",
          className,
        )}
      >
        <Info className="h-3 w-3" />
      </PopoverTrigger>
      <PopoverContent onClick={(e) => e.stopPropagation()}>
        {title && <div className="mb-1.5 text-[11px] font-semibold tracking-wide uppercase opacity-60">{title}</div>}
        <div className="space-y-1.5 text-[11px] leading-snug text-[var(--color-muted)]">{children}</div>
      </PopoverContent>
    </Popover>
  )
}

/** The start → end boundaries of a metric, as a definition list. */
export function MetricBoundaries({
  start,
  end,
  source,
  limitations,
}: {
  start: string
  end: string
  source?: string
  limitations?: string
}) {
  return (
    <>
      <div className="space-y-0.5 font-mono text-[10px] text-[var(--color-foreground)]">
        <div>
          <span className="opacity-50">start </span>
          {start}
        </div>
        <div>
          <span className="opacity-50">end&nbsp;&nbsp; </span>
          {end}
        </div>
        {source && (
          <div>
            <span className="opacity-50">source </span>
            {source.replace(/_/g, "-")}
          </div>
        )}
      </div>
      {limitations && <p>{limitations}</p>}
    </>
  )
}
