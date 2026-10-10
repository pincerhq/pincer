import { create } from "zustand"
import { persist } from "zustand/middleware"
import type { TokenPair } from "@/api/types"

interface AuthState {
  accessToken: string | null
  refreshToken: string | null
  /** `pincer_user_id` of the signed-in identity. */
  user: string | null
  apiUrl: string
  isConnected: boolean
  version: string | null
  /** False when the agent runs with PINCER_AUTH_DISABLED: no tokens needed. */
  authRequired: boolean
  setSession: (pair: TokenPair) => void
  setApiUrl: (url: string) => void
  setConnected: (connected: boolean, version?: string) => void
  /** Enter an agent that reported `auth_required: false` — no credentials. */
  connectWithoutAuth: (version?: string) => void
  logout: () => void
}

const SIGNED_OUT = {
  accessToken: null,
  refreshToken: null,
  user: null,
  isConnected: false,
  version: null,
  authRequired: true,
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      ...SIGNED_OUT,
      apiUrl: import.meta.env.VITE_API_URL || window.location.origin,
      setSession: (pair) =>
        set({
          accessToken: pair.access_token,
          refreshToken: pair.refresh_token,
          user: pair.pincer_user_id,
          isConnected: true,
          authRequired: true,
        }),
      setApiUrl: (apiUrl) => set({ apiUrl }),
      setConnected: (isConnected, version) =>
        set({ isConnected, version: version ?? null }),
      connectWithoutAuth: (version) =>
        set({ ...SIGNED_OUT, authRequired: false, isConnected: true, version: version ?? null }),
      logout: () => set({ ...SIGNED_OUT }),
    }),
    {
      name: "pincer-auth",
      version: 1,
      // v0 held the shared PINCER_DASHBOARD_TOKEN as `token`. It authenticates
      // nothing any more, so it is dropped and the user has to sign in.
      migrate: (persisted, version) => {
        const state = (persisted ?? {}) as Record<string, unknown>
        if (version < 1) {
          const rest = { ...state }
          delete rest.token
          return { ...rest, ...SIGNED_OUT } as unknown as AuthState
        }
        return state as unknown as AuthState
      },
    },
  ),
)

// Another tab refreshed (or signed out): pick up its tokens instead of using a
// refresh token the server may already have replaced.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === "pincer-auth") void useAuthStore.persist.rehydrate()
  })
}
