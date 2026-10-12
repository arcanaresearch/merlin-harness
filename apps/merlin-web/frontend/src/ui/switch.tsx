import * as SwitchPrimitive from '@radix-ui/react-switch'
import * as React from 'react'

import { cn } from './cn'

export interface SwitchProps extends React.ComponentProps<typeof SwitchPrimitive.Root> {
  size?: 'xs' | 'sm'
}

/**
 * The toggle. Bare, always with an aria-label — no bordered text wrapper.
 * Track: surface fill off / accent on; thumb: elevated white with a soft drop.
 */
export function Switch({ className, size = 'xs', ...props }: SwitchProps) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        'peer inline-flex shrink-0 cursor-pointer items-center rounded-full border border-transparent bg-surface-secondary shadow-[inset_0_0_0_1px_var(--ui-stroke-quaternary)] transition-colors duration-100',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--ui-focus-ring)',
        'data-[state=checked]:border-accent data-[state=checked]:bg-accent data-[state=checked]:shadow-none',
        'disabled:cursor-default disabled:opacity-50',
        size === 'xs' ? 'h-4 w-7' : 'h-5 w-9',
        className,
      )}
      data-slot="switch"
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          'pointer-events-none block rounded-full bg-white shadow-sm ring-0 transition-transform duration-100',
          size === 'xs' ? 'size-3 data-[state=checked]:translate-x-3.5 data-[state=unchecked]:translate-x-0.5' : 'size-4 data-[state=checked]:translate-x-4 data-[state=unchecked]:translate-x-0.5',
        )}
      />
    </SwitchPrimitive.Root>
  )
}
