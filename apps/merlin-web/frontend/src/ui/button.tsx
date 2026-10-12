import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'

import { cn } from './cn'
import { Loader } from './loader'

/**
 * THE button. Pick a `variant` + `size`; never pass height/padding/radius
 * overrides.
 *
 * Variants:
 *   default     accent fill, accent-foreground text
 *   destructive sev-red fill
 *   secondary   soft fill (bg-surface-quaternary) — the default non-primary look
 *   outline     transparent + 1px inset ring, no fill/shadow
 *   ghost       quiet chrome action
 *   text        boxless quiet inline ("Cancel", "Clear") — underlines on hover
 *   textStrong  bold always-underlined inline affordance ("Change", "Open logs")
 *
 * Sizes: default / xs / sm / lg / inline (flush, zero box) / micro, and the
 * icon family icon (40px) / icon-lg / icon-sm (32px) / icon-xs (24px) /
 * icon-titlebar (24px, titlebar geometry).
 *
 * Text buttons are square (no radius) and sized by padding + line-height.
 * Only icon buttons carry the shared 4px radius; `shape="pill"` is the ONE
 * sanctioned way to get a round button (the composer send/stop control) —
 * never `className="rounded-full"` at a call site.
 *
 * `busy` is the loading affordance: keeps the label (no width jump), prepends
 * the signature Loader, sets `aria-busy` and disables the button. Never swap
 * the label for "Saving…". With `asChild` the state is still announced
 * (`aria-busy` + `data-busy`), but the spinner and the `disabled` attribute
 * need a real `<button>` — Slot must receive exactly one child, and `disabled`
 * is meaningless on an `<a>`.
 *
 * Forwards its ref — required for every Radix `asChild` trigger (Tip,
 * DropdownMenuTrigger, DialogClose) to anchor its Popper correctly.
 */

// Text+icon actions underline the label on hover, not the glyph.
const TEXT_ACTION_ICON = '[&_svg]:no-underline'

const buttonVariants = cva(
  "inline-flex shrink-0 cursor-pointer select-none items-center justify-center gap-1.5 rounded-control whitespace-nowrap text-pb-caption font-medium shadow-none outline-none transition-colors duration-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--ui-focus-ring) disabled:pointer-events-none disabled:cursor-default disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5",
  {
    variants: {
      variant: {
        // A disabled primary drops the accent fill entirely rather than fading
        // it: a 40%-opacity accent still reads as "the button to press", and
        // measured 2.37:1 light / 2.87:1 dark for its own label. The label
        // takes the dedicated `accent-disabled-foreground` rung, which is tuned
        // to clear 3:1 on the disabled fill — generic disabled ink was legible
        // on chrome but not on this one.
        default:
          'bg-accent text-accent-foreground hover:bg-(--ui-accent)/90 disabled:bg-accent-disabled disabled:text-accent-disabled-foreground disabled:opacity-100',
        destructive: 'bg-sev-red text-white hover:bg-sev-red/90',
        secondary: 'bg-surface-quaternary text-content-primary hover:bg-control-hover',
        outline:
          'bg-transparent text-content-primary shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--ui-stroke-secondary)_60%,transparent)] hover:bg-control-hover',
        ghost: 'text-content-secondary hover:bg-control-hover hover:text-content-primary',
        text: `text-content-tertiary underline-offset-4 hover:text-content-primary hover:underline ${TEXT_ACTION_ICON}`,
        textStrong: `font-semibold text-content-secondary underline underline-offset-4 hover:text-content-primary ${TEXT_ACTION_ICON}`,
      },
      size: {
        default: 'px-3 py-1.5 has-[>svg]:px-2.5',
        xs: "gap-1 px-2 py-0.5 text-pb-micro has-[>svg]:px-1.5 [&_svg:not([class*='size-'])]:size-3",
        sm: 'px-2.5 py-1 has-[>svg]:px-2',
        lg: 'px-5 py-2 text-pb-body has-[>svg]:px-4',
        inline: 'h-auto gap-1 p-0 has-[>svg]:px-0',
        micro: "h-auto gap-0.5 px-1 py-0 text-pb-micro font-normal has-[>svg]:px-0.5 [&_svg:not([class*='size-'])]:size-3",
        icon: 'size-10 rounded-iconbtn',
        'icon-lg': 'size-10 rounded-iconbtn',
        'icon-sm': 'size-8 rounded-iconbtn',
        'icon-xs': "size-6 rounded-iconbtn [&_svg:not([class*='size-'])]:size-3",
        'icon-titlebar':
          'h-(--pb-titlebar-control-size) w-(--pb-titlebar-control-size) rounded-iconbtn [&_svg:not([class*="size-"])]:size-[13.9px]',
      },
      /** `pill` = fully round. The sanctioned alternative to a call-site radius override. */
      shape: {
        default: '',
        pill: 'rounded-full',
      },
    },
    compoundVariants: [
      { variant: 'textStrong', class: 'px-0 has-[>svg]:px-0' },
    ],
    defaultVariants: {
      variant: 'default',
      size: 'default',
      shape: 'default',
    },
  },
)

export interface ButtonProps
  extends React.ComponentProps<'button'>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
  /** Work in flight: shows the Loader, sets aria-busy, disables. Keep the label. */
  busy?: boolean
}

/** Loader diameter inside a button, per size. Matches the glyph metrics so the
 *  spinner sits on the same optical line as an icon would. */
const BUSY_LOADER_SIZE: Record<string, number> = {
  xs: 10,
  micro: 10,
  sm: 12,
  default: 12,
  inline: 12,
  lg: 14,
  icon: 16,
  'icon-lg': 16,
  'icon-sm': 14,
  'icon-xs': 12,
  'icon-titlebar': 12,
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ asChild = false, busy = false, children, className, disabled, shape = 'default', size = 'default', variant = 'default', ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    // Radix Slot requires EXACTLY ONE child — it clones that element and hands
    // it our props. Rendering `{loader}{children}` gives Slot a two-element
    // array (even when the first is null) and it throws "Slot failed to slot
    // onto its children". So build one child expression: the spinner rides
    // inside a Fragment with the label for a real <button>, and `asChild`
    // passes the caller's element through untouched.
    const showBusy = busy && !asChild
    const content = showBusy ? (
      <>
        <Loader className="shrink-0" decorative size={BUSY_LOADER_SIZE[size ?? 'default'] ?? 12} />
        {children}
      </>
    ) : (
      children
    )

    return (
      <Comp
        // `busy` on an asChild button still announces and marks the state; only
        // the spinner and the `disabled` attribute need a real <button>.
        aria-busy={busy || undefined}
        className={cn(buttonVariants({ shape, size, variant }), className)}
        data-busy={busy || undefined}
        data-size={size}
        data-slot="button"
        data-variant={variant}
        disabled={showBusy ? true : disabled}
        ref={ref}
        {...props}
      >
        {content}
      </Comp>
    )
  },
)
Button.displayName = 'Button'

export { Button, buttonVariants }
