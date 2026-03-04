import { lazy, Suspense } from "react"
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { Toaster } from "sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { ErrorBoundary } from "@/components/ErrorBoundary"
import { useAuthStore } from "@/stores/auth"
import { ROUTES } from "@/lib/constants"
import { AppLayout } from "@/components/layout/AppLayout"
import { LoginPage } from "@/pages/Login"
import { Skeleton } from "@/components/ui/skeleton"

const DashboardPage = lazy(() =>
  import("@/pages/Dashboard").then((m) => ({ default: m.DashboardPage })),
)
const CostsPage = lazy(() =>
  import("@/pages/Costs").then((m) => ({ default: m.CostsPage })),
)
const ConversationsPage = lazy(() =>
  import("@/pages/Conversations").then((m) => ({
    default: m.ConversationsPage,
  })),
)
const AuditPage = lazy(() =>
  import("@/pages/Audit").then((m) => ({ default: m.AuditPage })),
)
const SkillsPage = lazy(() =>
  import("@/pages/Skills").then((m) => ({ default: m.SkillsPage })),
)
const DoctorPage = lazy(() =>
  import("@/pages/Doctor").then((m) => ({ default: m.DoctorPage })),
)
const SettingsPage = lazy(() =>
  import("@/pages/Settings").then((m) => ({ default: m.SettingsPage })),
)

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
})

function AuthGuard({ children }: { children: React.ReactNode }) {
  const token = useAuthStore((s) => s.token)
  if (!token) return <Navigate to={ROUTES.LOGIN} replace />
  return <>{children}</>
}

function PageFallback() {
  return (
    <div className="p-6 space-y-4">
      <Skeleton className="h-8 w-48 bg-white/[0.06]" />
      <Skeleton className="h-32 w-full bg-white/[0.06]" />
      <Skeleton className="h-64 w-full bg-white/[0.06]" />
    </div>
  )
}

function PageWrapper({ children }: { children: React.ReactNode }) {
  return (
    <ErrorBoundary>
      <Suspense fallback={<PageFallback />}>{children}</Suspense>
    </ErrorBoundary>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={200}>
        <BrowserRouter>
          <Routes>
            <Route path={ROUTES.LOGIN} element={<LoginPage />} />
            <Route
              element={
                <AuthGuard>
                  <AppLayout />
                </AuthGuard>
              }
            >
              <Route
                path={ROUTES.DASHBOARD}
                element={<PageWrapper><DashboardPage /></PageWrapper>}
              />
              <Route
                path={ROUTES.COSTS}
                element={<PageWrapper><CostsPage /></PageWrapper>}
              />
              <Route
                path={ROUTES.CONVERSATIONS}
                element={<PageWrapper><ConversationsPage /></PageWrapper>}
              />
              <Route
                path={ROUTES.AUDIT}
                element={<PageWrapper><AuditPage /></PageWrapper>}
              />
              <Route
                path={ROUTES.SKILLS}
                element={<PageWrapper><SkillsPage /></PageWrapper>}
              />
              <Route
                path={ROUTES.DOCTOR}
                element={<PageWrapper><DoctorPage /></PageWrapper>}
              />
              <Route
                path={ROUTES.SETTINGS}
                element={<PageWrapper><SettingsPage /></PageWrapper>}
              />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
        <Toaster
          theme="dark"
          position="bottom-right"
          toastOptions={{
            style: {
              background: "#1a1a1a",
              border: "1px solid rgba(255,255,255,0.1)",
              color: "#fafafa",
            },
          }}
        />
      </TooltipProvider>
    </QueryClientProvider>
  )
}
