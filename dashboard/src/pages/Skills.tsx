import { useState } from "react"
import { PageContainer } from "@/components/layout/PageContainer"
import { SkillGrid } from "@/components/skills/SkillGrid"
import { InstallModal } from "@/components/skills/InstallModal"
import { useSkills, useSkillDelete } from "@/api/hooks/useSkills"
import { Button } from "@/components/ui/button"
import { Plus, RefreshCw } from "lucide-react"
import { toast } from "sonner"

export function SkillsPage() {
  const [installOpen, setInstallOpen] = useState(false)
  const { data, isLoading, refetch } = useSkills()
  const deleteMutation = useSkillDelete()

  const handleDelete = (name: string) => {
    if (!confirm(`Delete skill "${name}"?`)) return
    deleteMutation.mutate(name, {
      onSuccess: () => toast.success(`Deleted ${name}`),
      onError: () => toast.error(`Failed to delete ${name}`),
    })
  }

  return (
    <PageContainer title="Skills">
      <div className="flex items-center justify-between mb-6">
        <p className="text-sm text-[var(--color-muted)]">
          {data?.skills.length ?? 0} skills installed
        </p>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            className="border-[var(--color-border)]"
          >
            <RefreshCw className="h-3.5 w-3.5 mr-1.5" />
            Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => setInstallOpen(true)}
            className="bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90"
          >
            <Plus className="h-3.5 w-3.5 mr-1.5" />
            Install
          </Button>
        </div>
      </div>

      <SkillGrid
        skills={data?.skills ?? []}
        loading={isLoading}
        onDelete={handleDelete}
      />

      <InstallModal
        open={installOpen}
        onClose={() => setInstallOpen(false)}
      />
    </PageContainer>
  )
}
