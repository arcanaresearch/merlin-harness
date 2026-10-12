import { cn } from './cn'

export interface ProgressProps {
  /** Completion 0..1. Ignored when `indeterminate`. */
  value?: number
  /** Work is running but its extent is unknown — a travelling sliver. */
  indeterminate?: boolean
  /** Accessible name ("Scan progress"). */
  label?: string
  /** Fill tone. Defaults to the accent. */
  tone?: 'accent' | 'good' | 'warn' | 'bad'
  className?: string
}

const FILL_TONES = {
  accent: 'bg-accent',
  good: 'bg-sev-green',
  warn: 'bg-sev-yellow',
  bad: 'bg-sev-red',
} as const

function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0
  return Math.min(1, Math.max(0, value))
}

/**
 * The ONE progress bar: a 4px track with a tone fill. Determinate when `value`
 * is given, otherwise pass `indeterminate` for work whose extent isn't known
 * yet. Always a real `role="progressbar"` with aria values — never a bare
 * styled div, and never "N/M checks" as the only signal that work is moving.
 *
 * Indeterminate motion reuses the existing `animate-skeleton-pulse` keyframe
 * (no new CSS surface) and freezes under `prefers-reduced-motion` like every
 * other animation in the app (handled globally in styles.css).
 */
export function Progress({ className, indeterminate = false, label, tone = 'accent', value }: ProgressProps) {
  const pct = value === undefined ? undefined : clamp01(value) * 100

  return (
    <div
      aria-label={label}
      aria-valuemax={indeterminate ? undefined : 100}
      aria-valuemin={indeterminate ? undefined : 0}
      aria-valuenow={pct === undefined ? undefined : Math.round(pct)}
      className={cn('h-1 w-full overflow-hidden rounded-full bg-surface-tertiary', className)}
      data-slot="progress"
      role="progressbar"
    >
      {indeterminate ? (
        <div className={cn('h-full w-1/3 animate-skeleton-pulse rounded-full', FILL_TONES[tone])} />
      ) : (
        <div
          className={cn('h-full rounded-full transition-[width] duration-200 ease-out', FILL_TONES[tone])}
          style={{ width: `${pct ?? 0}%` }}
        />
      )}
    </div>
  )
}
