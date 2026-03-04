import { useEffect, useState } from "react"

interface ScoreRingProps {
  score: number
  size?: number
}

export function ScoreRing({ score, size = 160 }: ScoreRingProps) {
  const [animatedScore, setAnimatedScore] = useState(0)

  useEffect(() => {
    let frame: number
    const start = performance.now()
    const duration = 800

    function animate(now: number) {
      const elapsed = now - start
      const progress = Math.min(elapsed / duration, 1)
      const eased = 1 - Math.pow(1 - progress, 3)
      setAnimatedScore(Math.round(score * eased))
      if (progress < 1) frame = requestAnimationFrame(animate)
    }

    frame = requestAnimationFrame(animate)
    return () => cancelAnimationFrame(frame)
  }, [score])

  const radius = (size - 16) / 2
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (animatedScore / 100) * circumference

  const color =
    score >= 80
      ? "var(--color-success)"
      : score >= 50
        ? "var(--color-warning)"
        : "var(--color-danger)"

  return (
    <div className="flex flex-col items-center">
      <div className="relative" style={{ width: size, height: size }}>
        <svg
          className="-rotate-90"
          width={size}
          height={size}
          viewBox={`0 0 ${size} ${size}`}
        >
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="rgba(255,255,255,0.06)"
            strokeWidth="8"
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={color}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            className="transition-[stroke-dashoffset] duration-700 ease-out"
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span
            className="text-4xl font-semibold font-mono"
            style={{ color }}
          >
            {animatedScore}
          </span>
          <span className="text-xs text-[var(--color-muted)] mt-1">
            / 100
          </span>
        </div>
      </div>
    </div>
  )
}
