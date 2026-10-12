import * as React from 'react'

import { cn } from './cn'

export interface SegmentedControlOption<T extends string> {
  id: T
  label: string
  icon?: React.ComponentType<{ className?: string }>
}

export interface SegmentedControlProps<T extends string> {
  options: readonly SegmentedControlOption<T>[]
  value: T
  onChange: (id: T) => void
  className?: string
  /** Dims the whole track and blocks selection. */
  disabled?: boolean
}

/**
 * The choice control for small mutually-exclusive sets (scan mode, depth,
 * time range). Flat by design — a tinted track (bg-surface-tertiary) with a
 * raised active pill. Replaces radio piles and pill rows.
 *
 * **A single option is not a choice.** With `options.length === 1` this renders
 * a static labelled value instead of track-and-pill chrome — a control offering
 * nothing to pick reads as broken, and its lone "selected" segment invites a
 * click that does nothing. Screens get this for free; they don't branch.
 */
export function SegmentedControl<T extends string>({
  className,
  disabled = false,
  onChange,
  options,
  value,
}: SegmentedControlProps<T>) {
  const only = options.length === 1 ? options[0] : undefined

  if (only !== undefined) {
    const Icon = only.icon
    return (
      <div
        className={cn('inline-flex w-fit items-center gap-1 py-0.5 text-pb-micro font-medium text-content-primary', disabled && 'opacity-50', className)}
        data-slot="segmented-control"
        data-single-option="true"
      >
        {Icon ? <Icon className="size-3 text-content-tertiary" /> : null}
        {only.label}
      </div>
    )
  }

  return (
    <div
      className={cn(
        'inline-grid w-fit auto-cols-fr grid-flow-col gap-0.5 rounded-[5px] bg-surface-tertiary p-0.5',
        disabled && 'opacity-50',
        className,
      )}
      data-slot="segmented-control"
    >
      {options.map(({ id, label, icon: Icon }) => {
        const active = value === id
        return (
          <button
            aria-pressed={active}
            className={cn(
              'flex cursor-pointer select-none items-center justify-center gap-1 rounded-[3px] px-2.5 py-0.5 text-pb-micro font-medium transition-colors duration-100 disabled:cursor-default',
              // The active pill needs its own hairline in dark, where an
              // elevated fill on a tertiary track is only ~3 L* apart.
              // Unselected labels are SECONDARY, not tertiary: tertiary on the
              // track measured 3.28:1 light / 4.40:1 dark — both under AA.
              active
                ? 'bg-surface-elevated text-content-primary shadow-pb-sm ring-1 ring-stroke-secondary/60'
                : 'text-content-secondary hover:text-content-primary',
            )}
            disabled={disabled}
            key={id}
            onClick={() => onChange(id)}
            type="button"
          >
            {Icon ? <Icon className="size-3" /> : null}
            {label}
          </button>
        )
      })}
    </div>
  )
}
