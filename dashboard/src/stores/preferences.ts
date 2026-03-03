import { create } from "zustand"
import { persist } from "zustand/middleware"

interface PreferencesState {
  sidebarCollapsed: boolean
  defaultDateRange: 7 | 30 | 90
  toggleSidebar: () => void
  setDateRange: (range: 7 | 30 | 90) => void
}

export const usePreferencesStore = create<PreferencesState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      defaultDateRange: 7,
      toggleSidebar: () =>
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setDateRange: (defaultDateRange) => set({ defaultDateRange }),
    }),
    { name: "pincer-preferences" },
  ),
)
