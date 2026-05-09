export const ROUTES = {
  DASHBOARD: "/",
  COSTS: "/costs",
  CONVERSATIONS: "/conversations",
  AUDIT: "/audit",
  SKILLS: "/skills",
  DOCTOR: "/doctor",
  SETTINGS: "/settings",
  LOGIN: "/login",
} as const

export const CHART_COLORS = {
  primary: "#10b981",
  secondary: "#6366f1",
  tertiary: "#f59e0b",
  quaternary: "#ec4899",
  quinary: "#8b5cf6",
} as const

export const CHART_PALETTE = [
  CHART_COLORS.primary,
  CHART_COLORS.secondary,
  CHART_COLORS.tertiary,
  CHART_COLORS.quaternary,
  CHART_COLORS.quinary,
]

export const CHART_THEME = {
  grid: { stroke: "rgba(255,255,255,0.04)" },
  axis: { stroke: "rgba(255,255,255,0.1)", fontSize: 11, fill: "#888" },
  tooltip: {
    backgroundColor: "#1a1a1a",
    border: "1px solid rgba(255,255,255,0.1)",
    borderRadius: 8,
  },
} as const

export const REFETCH_INTERVALS = {
  STATUS: 10_000,
  COSTS: 15_000,
  AUDIT: 15_000,
  CONVERSATIONS: 15_000,
  SKILLS: 15_000,
} as const

export const CHANNEL_COLORS: Record<string, string> = {
  telegram: "#2AABEE",
  whatsapp: "#25D366",
  discord: "#5865F2",
  signal: "#3A76F0",
  web: "#888888",
  cli: "#888888",
}
