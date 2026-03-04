import { create } from "zustand"
import { persist } from "zustand/middleware"
import { resetApiClient } from "@/api/client"

interface AuthState {
  token: string | null
  apiUrl: string
  isConnected: boolean
  version: string | null
  setToken: (token: string) => void
  setApiUrl: (url: string) => void
  setConnected: (connected: boolean, version?: string) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      apiUrl: import.meta.env.VITE_API_URL || "http://localhost:8080",
      isConnected: false,
      version: null,
      setToken: (token) => {
        set({ token })
        resetApiClient()
      },
      setApiUrl: (apiUrl) => {
        set({ apiUrl })
        resetApiClient()
      },
      setConnected: (isConnected, version) =>
        set({ isConnected, version: version ?? null }),
      logout: () => {
        set({ token: null, isConnected: false, version: null })
        resetApiClient()
      },
    }),
    { name: "pincer-auth" },
  ),
)
