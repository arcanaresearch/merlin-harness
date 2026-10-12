import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import * as React from 'react'

import { cn } from './cn'
import { usePortalContainer } from './portal-container'

/**
 * Tooltips. Native `title=` is BANNED — unstyled, ~500ms OS delay, clashes
 * with the themed Tip. Wrap any single element in <Tip label="…">.
 *
 * Behavior contract:
 *   - 200ms hover delay so a cursor sweeping the chrome doesn't flash tips.
 *   - skipDelayDuration 0 — each tip independently honors its delay.
 *   - disableHoverableContent — tips are labels, not interactive surfaces;
 *     they must never stick open over -webkit-app-region: drag chrome.
 *   - No open animation — after the delay the chip appears at once.
 *   - Inverted chip: bg-foreground / text-background, 11px bold.
 *
 * Tips are for DISCOVERY. Only when hover teaches something the user can't
 * already see or infer. Never tip menu triggers, close buttons, or controls
 * whose visible label says the same thing.
 */

/** Default hover-open delay. Call sites needing an instant tip pass delayDuration={0}. */
const TIP_DELAY_MS = 200

/** True inside RootTooltipProvider — Tip falls back to a local provider when
 *  mounted outside the app root (unit tests, isolated surfaces). */
const HasTooltipProvider = React.createContext(false)

function TooltipProvider({
  delayDuration = 0,
  skipDelayDuration = 0,
  disableHoverableContent = true,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delayDuration={delayDuration}
      disableHoverableContent={disableHoverableContent}
      skipDelayDuration={skipDelayDuration}
      {...props}
    />
  )
}

function Tooltip({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />
}

/**
 * Trigger with a keyboard-only focus gate.
 *
 * Radix opens a tip on focus. Closing a menu or dialog restores focus to the
 * trigger that opened it — with a mouse, that flashed a tip the user never
 * asked for. Focus opens a tip only when the focus came from the keyboard
 * (`:focus-visible`); hover is unaffected.
 */
function TooltipTrigger({ onFocus, ...props }: React.ComponentProps<typeof TooltipPrimitive.Trigger>) {
  return (
    <TooltipPrimitive.Trigger
      data-slot="tooltip-trigger"
      onFocus={(event) => {
        onFocus?.(event)
        if (event.defaultPrevented) return
        const target = event.currentTarget
        if (!(target instanceof HTMLElement) || !target.matches(':focus-visible')) event.preventDefault()
      }}
      {...props}
    />
  )
}

function TooltipContent({
  className,
  collisionPadding = 8,
  sideOffset = 6,
  children,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  // Tips opened inside a Dialog portal into the dialog (see usePortalContainer).
  const container = usePortalContainer()

  return (
    <TooltipPrimitive.Portal container={container}>
      <TooltipPrimitive.Content
        // A tip flush to the window edge (⌘K landed at 1440.3px in a 1440px
        // viewport) reads as clipped. Keep an 8px gutter on every side.
        collisionPadding={collisionPadding}
        className={cn(
          'pb-portal pointer-events-none z-over-modal w-fit max-w-64 select-none',
          // No open animation (the chip appears at once after the delay); it
          // fades on close so a swept-past tip doesn't blink out.
          'data-[state=closed]:animate-fade-out',
          className,
        )}
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        {...props}
      >
        {/* Inverted chip. box-decoration-clone hugs each wrapped line.
            [&>*]:!inline-flex keeps a block-level label child from collapsing
            the chip's measured geometry. */}
        <span className="box-decoration-clone inline bg-(--ui-text-primary) px-1.5 py-1 font-kbd text-pb-micro font-bold leading-normal text-(--ui-bg-chrome) [&>*]:!inline-flex">
          {children}
        </span>
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  )
}

export interface TipProps extends Omit<React.ComponentProps<typeof TooltipPrimitive.Content>, 'content'> {
  label: React.ReactNode
  children: React.ReactNode
  delayDuration?: number
}

/** Drop-in replacement for native title=. Renders the child untouched when
 *  label is falsy. Open state is trigger-hover only — never sticky. */
function Tip({ label, children, delayDuration = TIP_DELAY_MS, ...props }: TipProps) {
  const provided = React.useContext(HasTooltipProvider)

  if (!label) return <>{children}</>

  const tip = (
    <Tooltip delayDuration={delayDuration} disableHoverableContent>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent {...props}>{label}</TooltipContent>
    </Tooltip>
  )

  return provided ? tip : <TooltipProvider delayDuration={delayDuration}>{tip}</TooltipProvider>
}

/** The app's single tooltip provider. Mount once at the shell root. */
function RootTooltipProvider({ children }: { children: React.ReactNode }) {
  return (
    <HasTooltipProvider.Provider value>
      <TooltipProvider delayDuration={0} disableHoverableContent>
        {children}
      </TooltipProvider>
    </HasTooltipProvider.Provider>
  )
}

export interface TipHintLabelProps {
  text: string
  hint?: string
}

/** Tooltip label with an optional trailing hotkey hint ("Toggle sidebar  ⌘B"). */
function TipHintLabel({ text, hint }: TipHintLabelProps) {
  if (!hint) return <>{text}</>
  return (
    <span className="inline-flex items-center gap-2">
      <span>{text}</span>
      <span className="opacity-55">{hint}</span>
    </span>
  )
}

/** Longer delay than a discovery Tip: revealing truncated text is a recovery,
 *  not a hint, so it shouldn't fire while the cursor is merely passing. */
const OVERFLOW_TIP_DELAY_MS = 600

export interface OverflowTipProps {
  /** The full text. Shown ONLY when the rendered child is actually clipped. */
  label: string
  /** Must render a single element that truncates (e.g. `.truncate`). */
  children: React.ReactElement
}

/**
 * Reveals the full text of a truncated label — the one way to read a clipped
 * sidebar/list title. Measures on EVERY pointer enter, so a label that grows
 * back into its box stops tipping, and one that shrinks starts.
 *
 * The `Tip` is always mounted and gated by `open`, never swapped in and out of
 * the tree: remounting the child under a parked cursor loses hover state and
 * makes the tip fire late or not at all.
 */
function OverflowTip({ children, label }: OverflowTipProps) {
  const provided = React.useContext(HasTooltipProvider)
  const [overflowing, setOverflowing] = React.useState(false)
  const [open, setOpen] = React.useState(false)

  const tip = (
    <Tooltip
      delayDuration={OVERFLOW_TIP_DELAY_MS}
      disableHoverableContent
      onOpenChange={(next) => setOpen(next && overflowing)}
      open={open}
    >
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )

  return (
    <span
      className="contents"
      data-slot="overflow-tip"
      onPointerEnter={(event) => {
        const node = event.currentTarget.firstElementChild
        setOverflowing(node instanceof HTMLElement && node.scrollWidth > node.clientWidth)
      }}
    >
      {provided ? tip : <TooltipProvider delayDuration={OVERFLOW_TIP_DELAY_MS}>{tip}</TooltipProvider>}
    </span>
  )
}

export { OverflowTip, RootTooltipProvider, Tip, TipHintLabel, Tooltip, TooltipContent, TooltipProvider, TooltipTrigger }
