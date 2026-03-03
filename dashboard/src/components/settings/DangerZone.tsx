import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from "@/components/ui/dialog"
import { useState } from "react"
import { AlertTriangle } from "lucide-react"

interface DangerZoneProps {
  onClearData: () => void
  onResetConfig: () => void
}

export function DangerZone({ onClearData, onResetConfig }: DangerZoneProps) {
  const [clearOpen, setClearOpen] = useState(false)
  const [resetOpen, setResetOpen] = useState(false)

  return (
    <>
      <div className="rounded-xl border border-red-500/20 bg-red-500/[0.03] p-6">
        <h3 className="text-sm font-medium text-[var(--color-danger)] flex items-center gap-2">
          <AlertTriangle className="h-4 w-4" />
          Danger Zone
        </h3>
        <p className="text-xs text-[var(--color-muted)] mt-1 mb-4">
          These actions are irreversible. Proceed with caution.
        </p>
        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setClearOpen(true)}
            className="border-red-500/20 text-[var(--color-danger)] hover:bg-red-500/10"
          >
            Clear All Data
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setResetOpen(true)}
            className="border-red-500/20 text-[var(--color-danger)] hover:bg-red-500/10"
          >
            Reset Configuration
          </Button>
        </div>
      </div>

      <Dialog open={clearOpen} onOpenChange={setClearOpen}>
        <DialogContent className="bg-[var(--color-card)] border-[var(--color-border)] text-[var(--color-foreground)]">
          <DialogHeader>
            <DialogTitle>Clear All Data</DialogTitle>
            <DialogDescription className="text-[var(--color-muted)]">
              This will permanently delete all conversations, memories, audit
              logs, and cost history. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setClearOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                onClearData()
                setClearOpen(false)
              }}
              className="bg-[var(--color-danger)] text-white hover:opacity-90"
            >
              Delete Everything
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={resetOpen} onOpenChange={setResetOpen}>
        <DialogContent className="bg-[var(--color-card)] border-[var(--color-border)] text-[var(--color-foreground)]">
          <DialogHeader>
            <DialogTitle>Reset Configuration</DialogTitle>
            <DialogDescription className="text-[var(--color-muted)]">
              This will reset all settings to their defaults. API keys and tokens
              will be removed. You&apos;ll need to reconfigure the agent.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setResetOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                onResetConfig()
                setResetOpen(false)
              }}
              className="bg-[var(--color-danger)] text-white hover:opacity-90"
            >
              Reset All Settings
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
