import { useState, type FormEvent } from "react"
import { HTTPError } from "ky"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { format } from "date-fns"
import { AlertTriangle, Check, Copy } from "lucide-react"
import { toast } from "sonner"
import { errorDetail, pincer, retryAfterText } from "@/api/client"
import { useAuthStore } from "@/stores/auth"
import { PageContainer } from "@/components/layout/PageContainer"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from "@/components/ui/dialog"

const SECTION = "rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] p-6"
const LABEL = "text-xs text-[var(--color-muted)] uppercase tracking-wider"
const FIELD = "mt-1.5 bg-[var(--color-card)] border-[var(--color-border)]"

function formatDate(value: string | null | undefined): string {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : format(date, "PPp")
}

/** Sentence for a failed account request; 429 gets its wait spelled out. */
async function failureMessage(err: unknown, fallback: string): Promise<string> {
  if (err instanceof HTTPError && err.response.status === 429) {
    const wait = retryAfterText(err.response)
    return wait ? `Too many attempts. Try again in ${wait}.` : "Too many attempts. Try again later."
  }
  return errorDetail(err, fallback)
}

export function AccountPage() {
  const user = useAuthStore((s) => s.user)
  const { data: me, isLoading } = useQuery({
    queryKey: ["me", user],
    queryFn: pincer.me,
    retry: false,
  })

  if (isLoading) {
    return (
      <PageContainer title="Account">
        <div className="max-w-3xl space-y-6">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-32 w-full bg-white/[0.06]" />
          ))}
        </div>
      </PageContainer>
    )
  }

  if (!me) {
    return (
      <PageContainer title="Account">
        <p className="text-sm text-[var(--color-muted)]">
          No signed-in identity. Accounts are only available when the agent
          requires sign-in.
        </p>
      </PageContainer>
    )
  }

  return (
    <PageContainer title="Account">
      <div className="max-w-3xl space-y-6">
        <section className={SECTION}>
          <h3 className="text-sm font-medium">Signed in as</h3>
          <p className="mt-3 text-lg font-semibold">{me.display_name || me.pincer_user_id}</p>
          <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-xs">
            <dt className="text-[var(--color-muted)]">Identity</dt>
            <dd className="font-mono">{me.pincer_user_id}</dd>
            <dt className="text-[var(--color-muted)]">Email</dt>
            <dd>{me.email || "—"}</dd>
            <dt className="text-[var(--color-muted)]">Timezone</dt>
            <dd>{me.timezone || "—"}</dd>
            <dt className="text-[var(--color-muted)]">Signed in with</dt>
            <dd>{me.auth_method === "api_key" ? "API key" : "Password"}</dd>
          </dl>
        </section>

        <ChangePassword />
        <ApiKeySection user={me.pincer_user_id} />
      </div>
    </PageContainer>
  )
}

function ChangePassword() {
  const setSession = useAuthStore((s) => s.setSession)
  const [current, setCurrent] = useState("")
  const [next, setNext] = useState("")
  const [confirm, setConfirm] = useState("")
  const [error, setError] = useState("")
  const [done, setDone] = useState(false)
  const [saving, setSaving] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setDone(false)
    if (next !== confirm) {
      setError("New password and confirmation do not match.")
      return
    }
    setError("")
    setSaving(true)
    try {
      // Every token issued before this call is dead now, ours included: the
      // returned pair is the only way to stay signed in.
      const pair = await pincer.changePassword(current, next)
      setSession(pair)
      setCurrent("")
      setNext("")
      setConfirm("")
      setDone(true)
    } catch (err) {
      setError(await failureMessage(err, "Could not change the password."))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className={SECTION}>
      <h3 className="text-sm font-medium">Change password</h3>
      <p className="text-xs text-[var(--color-muted)] mt-1">
        Changing it signs out every other session and device.
      </p>
      <form onSubmit={submit} className="mt-4 max-w-sm space-y-4">
        <div>
          <label htmlFor="account-current-password" className={LABEL}>
            Current password
          </label>
          <Input
            id="account-current-password"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            className={FIELD}
          />
        </div>
        <div>
          <label htmlFor="account-new-password" className={LABEL}>
            New password
          </label>
          <Input
            id="account-new-password"
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            autoComplete="new-password"
            className={FIELD}
          />
        </div>
        <div>
          <label htmlFor="account-confirm-password" className={LABEL}>
            Confirm new password
          </label>
          <Input
            id="account-confirm-password"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            className={FIELD}
          />
        </div>

        {error && (
          <p role="alert" className="text-xs text-[var(--color-danger)]">{error}</p>
        )}
        {done && (
          <p role="status" className="text-xs text-[var(--color-success)]">
            Password changed. Other sessions have been signed out.
          </p>
        )}

        <Button
          type="submit"
          size="sm"
          disabled={saving || !current || !next || !confirm}
          className="bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90"
        >
          {saving ? "Saving..." : "Change password"}
        </Button>
      </form>
    </section>
  )
}

function ApiKeySection({ user }: { user: string }) {
  const queryClient = useQueryClient()
  const queryKey = ["me", user, "api-key"]
  // Only ever the masked key: the query cache outlives this page.
  const { data: info, isLoading } = useQuery({ queryKey, queryFn: pincer.apiKey, retry: false })
  // The full key lives here and nowhere else — gone as soon as the page unmounts.
  const [fullKey, setFullKey] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState("")

  const generate = async (force: boolean) => {
    setWorking(true)
    setError("")
    setCopied(false)
    try {
      const created = await pincer.createApiKey(force)
      setFullKey(created.api_key)
      queryClient.setQueryData(queryKey, {
        exists: true,
        masked: created.masked,
        created_at: created.created_at,
      })
    } catch (err) {
      if (err instanceof HTTPError && err.response.status === 409) {
        // A key appeared since this page loaded (another tab or device).
        void queryClient.invalidateQueries({ queryKey })
      }
      setError(await failureMessage(err, "Could not generate an API key."))
    } finally {
      setWorking(false)
    }
  }

  const copy = async () => {
    if (!fullKey) return
    try {
      await navigator.clipboard.writeText(fullKey)
      setCopied(true)
    } catch {
      toast.error("Could not copy. Select the key and copy it manually.")
    }
  }

  const exists = info?.exists ?? false

  return (
    <section className={SECTION}>
      <h3 className="text-sm font-medium">API key</h3>
      <p className="text-xs text-[var(--color-muted)] mt-1">
        For scripts and integrations that act as you. One key per identity.
      </p>

      <div className="mt-4 flex items-center gap-4">
        {isLoading ? (
          <Skeleton className="h-5 w-48 bg-white/[0.06]" />
        ) : exists ? (
          <div className="text-xs">
            <span className="font-mono text-sm">{info?.masked}</span>
            <span className="ml-3 text-[var(--color-muted)]">
              Created {formatDate(info?.created_at)}
            </span>
          </div>
        ) : (
          <span className="text-sm text-[var(--color-muted)]">No API key yet</span>
        )}
        <Button
          variant="outline"
          size="sm"
          disabled={isLoading || working}
          onClick={() => (exists ? setConfirmOpen(true) : void generate(false))}
          className="ml-auto border-[var(--color-border)]"
        >
          {exists ? "Regenerate" : "Generate"}
        </Button>
      </div>

      {error && (
        <p role="alert" className="mt-3 text-xs text-[var(--color-danger)]">{error}</p>
      )}

      {fullKey && (
        <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/[0.05] p-4">
          <p className="flex items-center gap-2 text-xs text-[var(--color-warning)]">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            Store it now, it will not be shown again.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <code
              data-testid="api-key-full"
              className="flex-1 break-all rounded bg-black/30 px-3 py-2 font-mono text-xs select-all"
            >
              {fullKey}
            </code>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void copy()}
              className="border-[var(--color-border)]"
            >
              {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
        </div>
      )}

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="bg-[var(--color-card)] border-[var(--color-border)] text-[var(--color-foreground)]">
          <DialogHeader>
            <DialogTitle>Regenerate API key</DialogTitle>
            <DialogDescription className="text-[var(--color-muted)]">
              The current key stops working immediately. Anything still using
              it will fail until it is given the new key.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                setConfirmOpen(false)
                void generate(true)
              }}
              className="bg-[var(--color-danger)] text-white hover:opacity-90"
            >
              Regenerate
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}
