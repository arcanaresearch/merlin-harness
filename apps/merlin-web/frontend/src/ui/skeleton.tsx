import * as React from 'react'

import { cn } from './cn'

export interface SkeletonProps extends React.ComponentProps<'div'> {
  /** Circle variant for avatars / status dots. */
  circle?: boolean
}

/**
 * Shape-preserving placeholder. Fill: surface-tertiary, pulsing at 1.6s.
 * Size it to the content it stands in for (h-4 w-40 text line, size-6 dot) —
 * a skeleton must hold the layout the real content will occupy.
 */
export function Skeleton({ circle = false, className, ...props }: SkeletonProps) {
  return (
    <div
      aria-hidden
      className={cn('animate-skeleton-pulse bg-surface-tertiary', circle ? 'rounded-full' : 'rounded-control', className)}
      data-slot="skeleton"
      {...props}
    />
  )
}
