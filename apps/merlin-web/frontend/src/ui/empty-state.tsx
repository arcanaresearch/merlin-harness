import * as React from 'react'

import { cn } from './cn'

export interface EmptyStateProps {
  /** Short headline saying what isn't here yet ("No scans yet"). */
  title: React.ReactNode
  /** One sentence naming the verb that fills it. */
  description?: React.ReactNode
  /** Optional primary action (a Button) — the same verb as the description. */
  action?: React.ReactNode
  /** Opt-in glyph. Off by default: the same inbox mark on seven screens is
   *  noise, not information. */
  icon?: React.ReactNode
  /** Richer visual for a first-run moment (e.g. `<BrandMark/>`). Replaces `icon`. */
  illustration?: React.ReactNode
  className?: string
}

/**
 * THE empty state — page-level and list-level alike. One shape everywhere:
 *
 *   per-screen glyph (`icon`) → title `text-pb-section font-semibold`
 *   primary ink → ONE sentence `text-pb-caption` tertiary → at most one action.
 *
 * Give each screen its own glyph and its own words — "Nothing exported yet.
 * Export a report from any finished scan." beats a shared "No data". What is
 * NOT allowed: a bare grey line with a button, an `Alert` standing in for an
 * empty, `PanelEmpty` in a page body, or two empties on one screen.
 */
export function EmptyState({ action, className, description, icon, illustration, title }: EmptyStateProps) {
  const mark = illustration ?? icon

  return (
    <div className={cn('flex flex-col items-center justify-center px-6 py-14 text-center', className)} data-slot="empty-state">
      {mark ? <div className="mb-3 text-content-tertiary [&_svg]:size-7">{mark}</div> : null}
      <div className="text-pb-section font-semibold text-content-primary">{title}</div>
      {description ? <div className="mt-1 max-w-sm text-pb-caption text-content-tertiary">{description}</div> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  )
}
