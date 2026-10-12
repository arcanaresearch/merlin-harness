import * as React from 'react'

import { cn } from './cn'

export interface SectionLabelProps extends React.ComponentProps<'div'> {
  children: React.ReactNode
}

/**
 * The uppercase eyebrow that names a block inside a page — "Delivery",
 * "Coverage", "App". 10px semibold, wide tracking, tertiary ink.
 *
 * This is the ONE sanctioned 10px readable string in the library: it is a
 * short all-caps label, not prose, and the type scale's own note carves it out
 * (see Typography). Anything a user reads as a sentence takes a `text-pb-*`
 * step instead.
 *
 * It labels a GROUP. For a labelled value use `PanelDetail`; for a section
 * heading with actions use `PanelHeader`; for a screen title use `PageHeader`.
 */
export function SectionLabel({ children, className, ...props }: SectionLabelProps) {
  return (
    <div
      className={cn('pb-1.5 text-[10px] font-semibold uppercase leading-3 tracking-[0.07em] text-content-tertiary', className)}
      data-slot="section-label"
      {...props}
    >
      {children}
    </div>
  )
}
