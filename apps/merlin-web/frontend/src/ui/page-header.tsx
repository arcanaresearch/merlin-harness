import * as React from 'react'

import { Button } from './button'
import { cn } from './cn'
import { ChevronLeftGlyph } from './glyphs'

export interface PageHeaderProps {
  /** The screen's name, in sentence case. One per screen. */
  title: React.ReactNode
  /** One sentence saying what this screen is for. Optional. */
  description?: React.ReactNode
  /** Right-aligned primary/secondary actions. */
  actions?: React.ReactNode
  /** Renders a back affordance above the title (detail screens). */
  back?: { label: string; onClick: () => void }
  className?: string
}

/**
 * The ONE page title block. Every screen body starts with it, so page titles,
 * back links and header actions share exactly one geometry and hierarchy.
 *
 * Title is `text-pb-page` semibold with a hair of negative tracking;
 * description is `text-pb-caption` tertiary. Don't hand-roll an `<h1>` + row of
 * buttons at a call site, and don't restate the sidebar label as a subtitle.
 */
export function PageHeader({ actions, back, className, description, title }: PageHeaderProps) {
  return (
    <header className={cn('flex flex-col gap-2', className)} data-slot="page-header">
      {back ? (
        <div>
          <Button onClick={back.onClick} size="inline" variant="text">
            <ChevronLeftGlyph />
            {back.label}
          </Button>
        </div>
      ) : null}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="truncate text-pb-page font-semibold tracking-[-0.01em] text-content-primary">{title}</h1>
          {description ? <p className="mt-0.5 text-pb-caption text-content-tertiary">{description}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  )
}
