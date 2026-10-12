import * as React from 'react'

import { cn } from './cn'

export interface KbdProps extends React.ComponentProps<'kbd'> {
  /** `md` (default, 12px chip on a 22px cap) for surfaces the user reads;
   *  `sm` (10px/18px) for dense inline hints. */
  size?: 'md' | 'sm'
}

/**
 * Keyboard hint chip ("⌘K"). Always the native UI face (font-kbd), never the
 * theme mono. Render each key as its own chip — `<KbdGroup keys={['⌘','K']}/>`
 * does that for you.
 */
export function Kbd({ className, size = 'md', ...props }: KbdProps) {
  return (
    <kbd
      className={cn(
        'inline-flex select-none items-center justify-center rounded-[4px] border border-stroke-tertiary bg-surface-elevated px-1 font-kbd font-medium leading-none text-content-secondary shadow-[0_1px_0_color-mix(in_srgb,var(--ui-base)_12%,transparent)]',
        size === 'sm' ? 'h-[18px] min-w-[18px] text-[10px]' : 'h-[22px] min-w-[22px] text-pb-caption',
        className,
      )}
      data-size={size}
      data-slot="kbd"
      {...props}
    />
  )
}

export interface KbdGroupProps {
  /** Keys in press order, e.g. `['⌘', 'K']`. One chip each. */
  keys: readonly string[]
  size?: KbdProps['size']
  className?: string
}

/** A shortcut rendered as one chip per key. The only way to show a combo. */
export function KbdGroup({ className, keys, size }: KbdGroupProps) {
  return (
    <span className={cn('inline-flex items-center gap-0.5', className)} data-slot="kbd-group">
      {keys.map((key) => (
        <Kbd key={key} size={size}>
          {key}
        </Kbd>
      ))}
    </span>
  )
}
