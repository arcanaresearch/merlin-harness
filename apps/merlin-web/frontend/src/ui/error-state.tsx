import * as React from 'react'

import { cn } from './cn'
import { CopyButton } from './copy-button'
import { ErrorGlyph } from './glyphs'

export interface ErrorStateProps {
  /** What happened, in one line, in the user's words. Accepts nodes so dialog
   *  a11y primitives can flow through. */
  title: React.ReactNode
  /** Detail / recovery guidance — what to do next. */
  description?: React.ReactNode
  /** Recovery action (e.g. a retry Button). */
  action?: React.ReactNode
  /** Raw technical detail worth copying (a message, a code). Renders a
   *  CopyButton beside the title so the user can paste it into a support
   *  request instead of retyping it from a screenshot. */
  copyText?: string
  className?: string
}

/**
 * The ONE error look — canonical ErrorGlyph in sev-red, no background chip.
 * Used by the React boundary, in-dialog errors, and the boot-failure card.
 *
 * Title and description are `.pb-selectable`: the shell sets `user-select:
 * none`, and an error a user cannot copy is an error they have to retype.
 */
export function ErrorState({ action, className, copyText, description, title }: ErrorStateProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-1.5 px-6 py-16 text-center', className)} data-slot="error-state">
      <div className="mb-1 text-sev-red [&_svg]:size-6">
        <ErrorGlyph />
      </div>
      <div className="flex items-center gap-1">
        <div className="pb-selectable text-pb-section font-semibold text-content-primary">{title}</div>
        {copyText === undefined ? null : <CopyButton label="Copy error details" value={copyText} />}
      </div>
      {description ? <div className="pb-selectable max-w-md text-pb-caption text-content-secondary">{description}</div> : null}
      {action ? <div className="mt-3">{action}</div> : null}
    </div>
  )
}
