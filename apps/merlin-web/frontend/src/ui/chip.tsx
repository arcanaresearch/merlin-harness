import * as React from 'react'

import { cn } from './cn'

const CHIP_TONES = {
  muted: 'bg-surface-tertiary text-content-secondary',
  info: 'bg-sev-blue-soft text-sev-blue-text',
} as const

export interface ChipProps extends React.ComponentProps<'span'> {
  tone?: keyof typeof CHIP_TONES
  /**
   * `mono` (default) for machine values — ids, hashes, header names, code.
   * `text` for human-readable names ("Gradual escalation", "Plain text"), which
   * are words, not code: monospace on a phrase is a costume, and it put two
   * chip typographies in one panel.
   */
  variant?: 'mono' | 'text'
}

/**
 * Inline value chip. ONE geometry for all of them; don't re-declare
 * `rounded-control bg-surface-tertiary px-1.5` at a call site.
 *
 * Reach for mono ONLY when the value is something a machine produced and a
 * human would copy verbatim. For a status word use `PanelPill`; for a
 * multi-line payload use `CodeBlock`.
 */
export const Chip = React.forwardRef<HTMLSpanElement, ChipProps>(
  ({ className, tone = 'muted', variant = 'mono', ...props }, ref) => (
    <span
      className={cn(
        'pb-selectable inline-flex items-center rounded-control px-1.5 py-px text-pb-micro',
        variant === 'mono' ? 'font-pbmono' : 'font-medium',
        CHIP_TONES[tone],
        className,
      )}
      data-slot="chip"
      data-tone={tone}
      data-variant={variant}
      ref={ref}
      {...props}
    />
  ),
)
Chip.displayName = 'Chip'

export interface CodeBlockProps extends React.ComponentProps<'pre'> {
  /** Trailing controls floated top-right (typically a `CopyButton`). */
  actions?: React.ReactNode
}

/**
 * The ONE code / raw-payload slab: mono, scrollable, on the single code
 * surface. Background only — no border, no header, no language caption
 * (a caption says nothing a consumer can use).
 *
 * Selectable by design. Pair with a `CopyButton` via `actions` rather than
 * asking the user to drag-select.
 */
export function CodeBlock({ actions, children, className, ...props }: CodeBlockProps) {
  return (
    <div className="relative min-w-0" data-slot="code-block">
      <pre
        className={cn(
          // 11px mono on 20px leading: the micro step's own 16px leading packs
          // dense payloads too tightly to scan line by line.
          'pb-selectable pb-scroll min-w-0 overflow-x-auto rounded-menu bg-surface-code px-3 py-2 font-pbmono text-pb-micro leading-5 text-content-secondary',
          actions ? 'pr-9' : undefined,
          className,
        )}
        {...props}
      >
        {children}
      </pre>
      {actions ? <div className="absolute right-1.5 top-1.5">{actions}</div> : null}
    </div>
  )
}
