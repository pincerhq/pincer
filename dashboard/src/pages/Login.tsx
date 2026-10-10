import { useEffect, useState, type FormEvent } from "react"
import { HTTPError } from "ky"
import { useNavigate } from "react-router-dom"
import { useAuthStore } from "@/stores/auth"
import { pincer, retryAfterText } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

const HEALTH_PROBE_DELAY_MS = 300

function cleanUrl(url: string): string {
  return url.trim().replace(/\/$/, "") || window.location.origin
}

export function LoginPage() {
  const navigate = useNavigate()
  const auth = useAuthStore()
  const [identifier, setIdentifier] = useState("")
  const [password, setPassword] = useState("")
  const [url, setUrl] = useState(auth.apiUrl)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)
  // The agent at `openAgent.url` reported `auth_required: false` (local dev).
  const [openAgent, setOpenAgent] = useState<{ url: string; version: string } | null>(null)

  const baseUrl = cleanUrl(url)
  const authOptional = openAgent?.url === baseUrl

  // Ask the agent whether it wants a sign-in at all. Best effort: an
  // unreachable agent simply keeps the normal form.
  useEffect(() => {
    let cancelled = false
    const timer = setTimeout(() => {
      pincer
        .healthAt(baseUrl)
        .then((health) => {
          if (cancelled) return
          setOpenAgent(
            health.auth_required === false ? { url: baseUrl, version: health.version } : null,
          )
        })
        .catch(() => {
          if (!cancelled) setOpenAgent(null)
        })
    }, HEALTH_PROBE_DELAY_MS)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [baseUrl])

  const connect = async (e?: FormEvent) => {
    e?.preventDefault()
    setLoading(true)
    setError("")
    try {
      const pair = await pincer.login(baseUrl, identifier.trim(), password)
      const health = await pincer.healthAt(baseUrl).catch(() => null)
      auth.setApiUrl(baseUrl)
      auth.setSession(pair)
      auth.setConnected(true, health?.version)
      navigate("/")
    } catch (err) {
      if (err instanceof HTTPError && err.response.status === 401) {
        setError("Invalid name, email or password.")
      } else if (err instanceof HTTPError && err.response.status === 429) {
        const wait = retryAfterText(err.response)
        setError(
          wait
            ? `Too many failed attempts. Try again in ${wait}.`
            : "Too many failed attempts. Try again later.",
        )
      } else if (err instanceof HTTPError && err.response.status === 422) {
        setError("Enter your name or email and your password.")
      } else if (err instanceof HTTPError) {
        setError(`Agent responded with HTTP ${err.response.status}.`)
      } else {
        setError(
          `Could not reach the agent at ${baseUrl}. Check that it is running and that the URL matches the one in your address bar.`,
        )
      }
      auth.logout()
    } finally {
      setLoading(false)
    }
  }

  const continueWithoutAuth = () => {
    if (!openAgent) return
    auth.setApiUrl(openAgent.url)
    auth.connectWithoutAuth(openAgent.version)
    navigate("/")
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--color-background)]">
      <form onSubmit={connect} className="w-full max-w-sm space-y-8">
        <div className="text-center">
          <h1 className="text-4xl font-semibold tracking-tight">pincer</h1>
          <p className="mt-3 text-sm text-[var(--color-muted)]">
            Sign in to your Pincer agent
          </p>
        </div>

        <div className="space-y-4">
          <div>
            <label
              htmlFor="login-url"
              className="text-xs text-[var(--color-muted)] uppercase tracking-wider"
            >
              Agent URL
            </label>
            <Input
              id="login-url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="http://localhost:8080"
              className="mt-1.5 bg-[var(--color-card)] border-[var(--color-border)] focus:border-[var(--color-accent)] focus:ring-[var(--color-accent)]"
            />
          </div>
          <div>
            <label
              htmlFor="login-identifier"
              className="text-xs text-[var(--color-muted)] uppercase tracking-wider"
            >
              Name or email
            </label>
            <Input
              id="login-identifier"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              autoComplete="username"
              autoCapitalize="none"
              className="mt-1.5 bg-[var(--color-card)] border-[var(--color-border)] focus:border-[var(--color-accent)] focus:ring-[var(--color-accent)]"
            />
          </div>
          <div>
            <label
              htmlFor="login-password"
              className="text-xs text-[var(--color-muted)] uppercase tracking-wider"
            >
              Password
            </label>
            <Input
              id="login-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              className="mt-1.5 bg-[var(--color-card)] border-[var(--color-border)] focus:border-[var(--color-accent)] focus:ring-[var(--color-accent)]"
            />
          </div>

          {error && (
            <p role="alert" className="text-xs text-[var(--color-danger)]">{error}</p>
          )}

          <Button
            type="submit"
            disabled={loading || !identifier.trim() || !password}
            className="w-full bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90 transition-opacity"
          >
            {loading ? "Signing in..." : "Sign in"}
          </Button>

          {authOptional && (
            <div className="space-y-1.5">
              <Button
                type="button"
                variant="outline"
                onClick={continueWithoutAuth}
                className="w-full border-[var(--color-border)]"
              >
                Continue without signing in
              </Button>
              <p className="text-center text-xs text-[var(--color-muted)]">
                This agent runs with authentication disabled.
              </p>
            </div>
          )}
        </div>

        <p className="text-center text-xs text-[var(--color-muted)]/60">
          Set an identity&apos;s password with{" "}
          <code className="font-mono">pincer identity set-password &lt;name&gt;</code>
        </p>
      </form>
    </div>
  )
}
