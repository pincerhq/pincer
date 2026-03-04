import { Component, type ErrorInfo, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { AlertTriangle, RefreshCw } from "lucide-react"

interface Props {
  children: ReactNode
  fallback?: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info)
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback

      return (
        <div className="flex flex-col items-center justify-center py-20 px-6">
          <AlertTriangle className="h-8 w-8 text-[var(--color-danger)] mb-4" />
          <h2 className="text-lg font-medium mb-2">Something went wrong</h2>
          <p className="text-sm text-[var(--color-muted)] mb-4 text-center max-w-md">
            {this.state.error?.message ?? "An unexpected error occurred."}
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => this.setState({ hasError: false, error: null })}
            className="border-[var(--color-border)]"
          >
            <RefreshCw className="h-3.5 w-3.5 mr-2" />
            Try Again
          </Button>
        </div>
      )
    }

    return this.props.children
  }
}
