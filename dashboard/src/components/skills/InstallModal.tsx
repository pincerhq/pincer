import { useState } from "react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { ScanResult } from "./ScanResult"
import { useSkillScan, useSkillInstall } from "@/api/hooks/useSkills"
import { Loader2 } from "lucide-react"
import { toast } from "sonner"

interface InstallModalProps {
  open: boolean
  onClose: () => void
}

export function InstallModal({ open, onClose }: InstallModalProps) {
  const [url, setUrl] = useState("")
  const scan = useSkillScan()
  const install = useSkillInstall()

  const handleScan = () => {
    if (!url.trim()) return
    scan.mutate(url.trim())
  }

  const handleInstall = () => {
    install.mutate(url.trim(), {
      onSuccess: (data) => {
        toast.success(`Installed ${data.name} v${data.version}`)
        setUrl("")
        scan.reset()
        onClose()
      },
      onError: () => {
        toast.error("Failed to install skill")
      },
    })
  }

  const handleClose = () => {
    setUrl("")
    scan.reset()
    onClose()
  }

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="bg-[var(--color-card)] border-[var(--color-border)] text-[var(--color-foreground)]">
        <DialogHeader>
          <DialogTitle>Install Skill</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div>
            <label className="text-xs text-[var(--color-muted)] uppercase tracking-wider">
              Skill URL or Path
            </label>
            <div className="flex gap-2 mt-1.5">
              <Input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://github.com/user/skill or /path/to/skill"
                className="bg-[var(--color-background)] border-[var(--color-border)]"
                onKeyDown={(e) => e.key === "Enter" && handleScan()}
              />
              <Button
                onClick={handleScan}
                disabled={!url.trim() || scan.isPending}
                variant="outline"
                className="border-[var(--color-border)] shrink-0"
              >
                {scan.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  "Scan"
                )}
              </Button>
            </div>
          </div>

          {scan.data && <ScanResult result={scan.data} />}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={handleClose}>
            Cancel
          </Button>
          <Button
            onClick={handleInstall}
            disabled={
              !scan.data || scan.data.verdict === "fail" || install.isPending
            }
            className="bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90"
          >
            {install.isPending ? (
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
            ) : null}
            Install
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
