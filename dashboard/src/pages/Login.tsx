import { useState, type FormEvent } from "react"
import { useNavigate } from "react-router-dom"
import { useAuthStore } from "@/stores/auth"
import { pincer } from "@/api/client"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

export function LoginPage() {
  const navigate = useNavigate()
  const auth = useAuthStore()
  const [token, setToken] = useState("")
  const [url, setUrl] = useState(auth.apiUrl)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(false)

  const connect = async (e?: FormEvent) => {
    e?.preventDefault()
    setLoading(true)
    setError("")
    try {
      const baseUrl = url.trim().replace(/\/$/, "") || window.location.origin
      const status = await pincer.validateToken(baseUrl, token)
      auth.setApiUrl(baseUrl)
      auth.setToken(token)
      const version =
        typeof status === "object" && status && "version" in status
          ? String((status as { version?: string }).version ?? "0.5.0")
          : "0.5.0"
      auth.setConnected(true, version)
      navigate("/")
    } catch {
      setError("Invalid token. Check your PINCER_DASHBOARD_TOKEN.")
      auth.logout()
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--color-background)]">
      <form onSubmit={connect} className="w-full max-w-sm space-y-8">
        <div className="text-center">
          <h1 className="text-4xl font-semibold tracking-tight">pincer</h1>
          <p className="mt-3 text-sm text-[var(--color-muted)]">
            Connect to your Pincer agent
          </p>
        </div>

        <div className="space-y-4">
          <div>
            <label className="text-xs text-[var(--color-muted)] uppercase tracking-wider">
              Agent URL
            </label>
            <Input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="http://localhost:8080"
              className="mt-1.5 bg-[var(--color-card)] border-[var(--color-border)] focus:border-[var(--color-accent)] focus:ring-[var(--color-accent)]"
            />
          </div>
          <div>
            <label className="text-xs text-[var(--color-muted)] uppercase tracking-wider">
              Dashboard Token
            </label>
            <Input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Enter your PINCER_DASHBOARD_TOKEN"
              className="mt-1.5 bg-[var(--color-card)] border-[var(--color-border)] focus:border-[var(--color-accent)] focus:ring-[var(--color-accent)]"
            />
          </div>

          {error && (
            <p className="text-xs text-[var(--color-danger)]">{error}</p>
          )}

          <Button
            type="submit"
            disabled={loading || !token}
            className="w-full bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:opacity-90 transition-opacity"
          >
            {loading ? "Connecting..." : "Connect"}
          </Button>
        </div>

        <p className="text-center text-xs text-[var(--color-muted)]/60">
          Set PINCER_DASHBOARD_TOKEN in your agent&apos;s .env file
        </p>
      </form>
    </div>
  )
}
