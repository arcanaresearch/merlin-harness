import * as React from 'react'

import { cn } from './cn'

const DOT_TONES = {
  good: 'bg-sev-green',
  bad: 'bg-sev-red',
  warn: 'bg-sev-yellow',
  info: 'bg-sev-blue',
  muted: 'bg-content-quaternary',
} as const

export interface StatusDotProps extends React.ComponentProps<'span'> {
  tone?: keyof typeof DOT_TONES
  /**
   * Attention beat when something CHANGED. FINITE — three cycles, then rest.
   * A permanently pinging dot is decoration, not signal.
   */
  pulse?: boolean
  /**
   * Work is actively running right now — the only case that loops forever.
   * "Connected" / "Ready" is a state, not live work: leave this off.
   */
  live?: boolean
  /** Short status word rendered after the dot. A dot is never the only signal. */
  label?: string
}

/**
 * Connection / scan status dot. 6px, severity tone.
 *
 * Pass `label` so the state is readable, not just coloured — that is the
 * primitive's contract. If a surface truly has no room for the word, give the
 * dot an `aria-label` at the call site; colour alone fails both accessibility
 * and a glance.
 */
export function StatusDot({ className, label, live = false, pulse = false, tone = 'muted', ...props }: StatusDotProps) {
  const mark = (
    <span className="relative inline-flex size-1.5 shrink-0">
      {/* Only genuinely live work gets the expanding ring, and only forever
          because the work itself is ongoing. */}
      {live ? <span className={cn('absolute inline-flex size-full animate-ping rounded-full opacity-60', DOT_TONES[tone])} /> : null}
      <span
        className={cn('relative inline-flex size-1.5 rounded-full', !live && pulse && 'animate-pulse-finite', DOT_TONES[tone])}
      />
    </span>
  )

  if (label === undefined) {
    return (
      <span className={cn('relative inline-flex shrink-0', className)} data-slot="status-dot" data-tone={tone} {...props}>
        {mark}
      </span>
    )
  }

  return (
    <span className={cn('inline-flex items-center gap-1.5', className)} data-slot="status-dot" data-tone={tone} {...props}>
      {mark}
      <span className="text-pb-micro text-content-tertiary">{label}</span>
    </span>
  )
}
