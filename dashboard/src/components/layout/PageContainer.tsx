import { useEffect, useRef, type ReactNode } from "react"

interface PageContainerProps {
  title?: string
  children: ReactNode
}

export function PageContainer({ title, children }: PageContainerProps) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (title) document.title = `${title} — Pincer`
    else document.title = "Pincer"
  }, [title])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    el.classList.add("page-enter")
    requestAnimationFrame(() => {
      el.classList.add("page-enter-active")
      el.classList.remove("page-enter")
    })
  }, [])

  return (
    <div ref={ref} className="p-6">
      {children}
    </div>
  )
}
