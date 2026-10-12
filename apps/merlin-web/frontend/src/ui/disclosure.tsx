import * as React from 'react'

import { cn } from './cn'
import { ChevronRightGlyph } from './glyphs'

export interface DisclosureProps {
  /** The toggle text — "Advanced", "Details", "What this scan checked". */
  label: React.ReactNode
  /** Starting state when uncontrolled. Ignored once `open` is supplied. */
  defaultOpen?: boolean
  /**
   * Controlled open state. Supply it (with `onOpenChange`) when the parent owns
   * the toggle — e.g. a screen that opens the block from elsewhere, or keeps
   * several in sync. Leave undefined and the primitive owns its own state.
   */
  open?: boolean
  /** Fires with the requested next state. Called in BOTH modes, so an
   *  uncontrolled consumer can still observe without taking ownership. */
  onOpenChange?: (open: boolean) => void
  children: React.ReactNode
  className?: string
}

/**
 * The ONE progressive-disclosure control: a flat text toggle with a caret that
 * rotates 90° when open. No box, no native `<details>` marker, one caret
 * direction everywhere.
 *
 * This is where ids, hashes, enum values, file paths and raw payloads live —
 * never in the primary surface. Keep the label a consumer noun ("Details"),
 * not a jargon one.
 *
 * Uncontrolled by default; pass `open` + `onOpenChange` to control it.
 */
export function Disclosure({
  children,
  className,
  defaultOpen = false,
  label,
  onOpenChange,
  open: openProp,
}: DisclosureProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = React.useState(defaultOpen)
  // Controlled the moment `open` is supplied. The internal state is then never
  // written, so the parent stays the single source of truth and the two cannot
  // drift apart.
  const controlled = openProp !== undefined
  const open = controlled ? openProp : uncontrolledOpen

  return (
    <div className={cn('flex flex-col gap-2', className)} data-slot="disclosure" data-state={open ? 'open' : 'closed'}>
      <button
        aria-expanded={open}
        className="flex w-fit cursor-pointer items-center gap-1 text-pb-caption font-medium text-content-tertiary transition-colors duration-100 hover:text-content-primary"
        data-slot="disclosure-trigger"
        onClick={() => {
          const next = !open
          if (!controlled) setUncontrolledOpen(next)
          onOpenChange?.(next)
        }}
        type="button"
      >
        <ChevronRightGlyph className={cn('size-3 transition-transform duration-100', open && 'rotate-90')} />
        {label}
      </button>
      {open ? (
        <div className="min-w-0" data-slot="disclosure-content">
          {children}
        </div>
      ) : null}
    </div>
  )
}
