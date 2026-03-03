import { PageContainer } from "@/components/layout/PageContainer"
import { SettingsForm } from "@/components/settings/SettingsForm"
import { DangerZone } from "@/components/settings/DangerZone"
import { useSettings, useUpdateSettings } from "@/api/hooks/useSettings"
import { Skeleton } from "@/components/ui/skeleton"
import { toast } from "sonner"
import type { Settings } from "@/api/types"

export function SettingsPage() {
  const { data, isLoading } = useSettings()
  const updateMutation = useUpdateSettings()

  const handleSave = (updated: Partial<Settings>) => {
    updateMutation.mutate(updated, {
      onSuccess: () => toast.success("Settings saved"),
      onError: () => toast.error("Failed to save settings"),
    })
  }

  const handleClearData = () => {
    toast.success("Data cleared (not implemented in backend)")
  }

  const handleResetConfig = () => {
    toast.success("Configuration reset (not implemented in backend)")
  }

  if (isLoading) {
    return (
      <PageContainer title="Settings">
        <div className="max-w-3xl space-y-6">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-32 w-full bg-white/[0.06]" />
          ))}
        </div>
      </PageContainer>
    )
  }

  if (!data) {
    return (
      <PageContainer title="Settings">
        <p className="text-sm text-[var(--color-muted)]">
          Unable to load settings. Is the agent running?
        </p>
      </PageContainer>
    )
  }

  return (
    <PageContainer title="Settings">
      <div className="max-w-3xl">
        <SettingsForm
          settings={data}
          onSave={handleSave}
          saving={updateMutation.isPending}
        />

        <div className="mt-12">
          <DangerZone
            onClearData={handleClearData}
            onResetConfig={handleResetConfig}
          />
        </div>
      </div>
    </PageContainer>
  )
}
