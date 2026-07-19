import { useParams, useNavigate } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { pincer } from "@/api/client"
import { PageContainer } from "@/components/layout/PageContainer"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"
import { ArrowLeft, FileText } from "lucide-react"
import { ROUTES } from "@/lib/constants"

export function SkillDetailPage() {
  const { name = "" } = useParams<{ name: string }>()
  const navigate = useNavigate()

  const { data, isLoading, error } = useQuery({
    queryKey: ["skill", name],
    queryFn: () => pincer.skill(name),
    enabled: !!name,
    staleTime: 60_000,
  })

  return (
    <PageContainer title="">
      {/* Back link */}
      <button
        onClick={() => navigate(ROUTES.SKILLS)}
        className="flex items-center gap-1.5 text-sm text-[var(--color-muted)] hover:text-[var(--color-foreground)] mb-6 transition-colors"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Skills
      </button>

      {isLoading && (
        <div className="space-y-4">
          <Skeleton className="h-24 w-full bg-white/[0.06] rounded-xl" />
          <Skeleton className="h-64 w-full bg-white/[0.06] rounded-xl" />
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-8 text-center text-[var(--color-muted)]">
          <p className="text-sm">Skill not found.</p>
        </div>
      )}

      {data && (
        <div className="space-y-6">
          {/* Header card */}
          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6">
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className="text-lg font-semibold">{data.name}</h1>
              <span className="text-[10px] font-semibold uppercase tracking-wider px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-400">
                {data.root}
              </span>
              <span
                className={cn(
                  "text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded-full",
                  data.status === "active"
                    ? "bg-[var(--color-success)]/15 text-[var(--color-success)]"
                    : "bg-white/[0.06] text-[var(--color-muted)]",
                )}
              >
                {data.status}
              </span>
            </div>
            <p className="text-sm text-[var(--color-muted)] mt-2">{data.description}</p>
          </div>

          {/* SKILL.md body */}
          <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-5">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-muted)] mb-4">
              SKILL.md
            </h2>
            <pre className="text-xs leading-relaxed text-[var(--color-foreground)] whitespace-pre-wrap break-words font-mono">
              {data.body}
            </pre>
          </div>

          {/* Other files in the skill's directory */}
          {data.files.length > 0 && (
            <div className="rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] overflow-hidden">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--color-muted)] px-4 pt-4 pb-2">
                Other Files ({data.files.length})
              </h2>
              <div className="divide-y divide-[var(--color-border)]">
                {data.files.map((file) => (
                  <div key={file} className="px-4 py-2.5 flex items-center gap-2.5">
                    <FileText className="h-3.5 w-3.5 text-[var(--color-muted)] shrink-0" />
                    <span className="text-xs font-mono text-[var(--color-muted)]">{file}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </PageContainer>
  )
}
