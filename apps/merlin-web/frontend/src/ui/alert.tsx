import * as React from 'react'

import { cn } from './cn'

export type AlertTone = 'error' | 'info' | 'success' | 'warning'

export interface AlertProps {
  /** Severity. Drives fill + text colour and the a11y role. */
  tone?: AlertTone
  /** Optional bold first line. Omit for a single-sentence notice. */
  title?: React.ReactNode
  children?: React.ReactNode
  /** Trailing action (a `text`/`textStrong` Button) — the recovery verb. */
  action?: React.ReactNode
  className?: string
}

const TONE_STYLE: Record<AlertTone, string> = {
  error: 'bg-sev-red-soft text-sev-red-text',
  warning: 'bg-sev-yellow-soft text-sev-yellow-text',
  info: 'bg-sev-blue-soft text-sev-blue-text',
  success: 'bg-sev-green-soft text-sev-green-text',
}

/**
 * The ONE inline notice. A soft severity slab with no icon chip — the fill and
 * the words carry the severity, so a row of notices doesn't turn into a row of
 * competing marks.
 *
 *   <Alert tone="warning">Exports are turned off for this account.</Alert>
 *   <Alert tone="error" title="Couldn’t save your key" action={<Button …/>}>…</Alert>
 *
 * Text is `.pb-selectable` so a user can copy an error out of it. Errors get
 * `role="alert"`, everything else `role="status"`. Never hand-roll a
 * `rounded-menu bg-sev-*-soft` box at a call site; never put an id, hash or
 * file path in here (put it behind `Disclosure` or a `CopyButton`).
 */
export function Alert({ action, children, className, title, tone = 'info' }: AlertProps) {
  return (
    <div
      className={cn(
        'pb-selectable flex items-start gap-3 rounded-menu px-3 py-2 text-pb-caption',
        TONE_STYLE[tone],
        className,
      )}
      data-slot="alert"
      data-tone={tone}
      role={tone === 'error' ? 'alert' : 'status'}
    >
      <div className="min-w-0 flex-1">
        {title ? <div className="font-semibold">{title}</div> : null}
        {children ? <div className={cn(title ? 'mt-0.5 opacity-90' : undefined)}>{children}</div> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  )
}
