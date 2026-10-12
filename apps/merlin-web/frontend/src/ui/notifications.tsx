import * as React from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { Button } from './button'
import { cn } from './cn'
import { CheckGlyph, ErrorGlyph, InfoGlyph, WarningGlyph, XGlyph } from './glyphs'

/**
 * Two-stack toasts.
 *
 *   top-center   IMPORTANT stack (errors, action-required). Collapsible — when
 *                more than one is queued the stack folds to the latest plus a
 *                "N more" expander. Never auto-dismisses.
 *   bottom-right AMBIENT stack (info/success). Auto-dismisses after 5s.
 *
 * Surfaces ride shadow-float + the --stroke-float hairline on the elevated
 * popover fill; entrance is toast-in-top/bottom at 200ms.
 *
 * Usage: wrap the shell in <ToastProvider>, render <ToastStacks/> once (the
 * AppShell does both), and call `toast.push(...)` via useToasts().
 */

export type ToastKind = 'error' | 'info' | 'success' | 'warning'
export type ToastPlacement = 'top-center' | 'bottom-right'

/** One follow-up verb on a toast ("Reveal in Finder", "Undo"). One per toast —
 *  a notification is not a menu. */
export interface ToastAction {
  label: string
  onClick: () => void
}

export interface ToastItem {
  id: string
  kind: ToastKind
  title: string
  description?: string
  action?: ToastAction
  placement: ToastPlacement
  createdAt: number
}

interface ToastInput {
  kind: ToastKind
  title: string
  description?: string
  /** Follow-up verb rendered at the card's right. Running it dismisses the toast. */
  action?: ToastAction
  /** Defaults: error/warning → top-center (important), info/success → bottom-right (ambient). */
  placement?: ToastPlacement
}

const AMBIENT_TTL_MS = 5_000

interface ToastContextValue {
  toasts: ToastItem[]
  push: (input: ToastInput) => string
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

let nextToastId = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])

  const dismiss = useCallback((id: string) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback((input: ToastInput) => {
    const id = `toast-${++nextToastId}`
    const placement = input.placement ?? (input.kind === 'error' || input.kind === 'warning' ? 'top-center' : 'bottom-right')
    const toast: ToastItem = { id, createdAt: Date.now(), placement, ...input }
    setToasts((current) => [toast, ...current])
    return id
  }, [])

  const value = useMemo(() => ({ dismiss, push, toasts }), [dismiss, push, toasts])
  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>
}

export function useToasts(): { push: (input: ToastInput) => string; dismiss: (id: string) => void } {
  const context = useContext(ToastContext)
  if (context === null) throw new Error('useToasts must be used inside <ToastProvider>')
  return { dismiss: context.dismiss, push: context.push }
}

const KIND_STYLE: Record<ToastKind, { icon: React.ComponentType<{ className?: string }>; iconClass: string }> = {
  error: { icon: ErrorGlyph, iconClass: 'text-sev-red' },
  warning: { icon: WarningGlyph, iconClass: 'text-sev-orange' },
  info: { icon: InfoGlyph, iconClass: 'text-content-tertiary' },
  success: { icon: CheckGlyph, iconClass: 'text-sev-green' },
}

/** How long the exit animation runs before the node is unmounted. */
const EXIT_MS = 150

interface ToastCardProps {
  toast: ToastItem
  onDismiss: () => void
  /** Auto-dismiss after this many ms. Omit for stacks that never expire. */
  ttlMs?: number
  /** The stack asked this card to go (e.g. "Clear all"). */
  closing?: boolean
}

/**
 * ONE exit path. Every removal — the ✕, the ambient TTL, "Clear all" — goes
 * through `leave()`, so a toast always animates out before it unmounts; a card
 * that vanishes mid-glance reads as a glitch.
 */
function ToastCard({ closing = false, onDismiss, toast, ttlMs }: ToastCardProps) {
  const style = KIND_STYLE[toast.kind]
  const Icon = style.icon
  // A card told to close at mount starts ALREADY leaving, so a folded toast
  // revealed by "Clear all" never paints its entrance for one frame first.
  const [leaving, setLeaving] = useState(closing)
  const timer = useRef<number | undefined>(undefined)
  const scheduled = useRef(false)

  // The stack rebuilds `onDismiss` on every render (the context value changes
  // on every push and dismiss). Held in a ref so `leave` can be referentially
  // STABLE — a `leave` that changed identity restarted the TTL timer below on
  // every render, and under steady traffic an ambient toast never expired.
  const onDismissRef = useRef(onDismiss)
  useEffect(() => {
    onDismissRef.current = onDismiss
  })

  // Guard and cleanup must AGREE. Under StrictMode the mount effects run,
  // clean up, then run again — clearing the timer while leaving
  // `scheduled.current` true would make `leave()` a permanent no-op, stranding
  // the card `pointer-events-none` with the stack unable to re-fold. Undo the
  // whole scheduling here; the `closing`/TTL effects re-drive it on the second
  // pass.
  useEffect(
    () => () => {
      window.clearTimeout(timer.current)
      timer.current = undefined
      scheduled.current = false
    },
    [],
  )

  const leave = useCallback(() => {
    if (scheduled.current) return
    scheduled.current = true
    setLeaving(true)
    timer.current = window.setTimeout(() => onDismissRef.current(), EXIT_MS)
  }, [])

  useEffect(() => {
    if (closing) leave()
  }, [closing, leave])

  useEffect(() => {
    if (ttlMs === undefined) return undefined
    const ttl = window.setTimeout(leave, ttlMs)
    return () => window.clearTimeout(ttl)
    // `leave` is stable for the card's whole life (useCallback with no deps),
    // so the countdown is keyed only on the toast's identity and its TTL.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [toast.id, ttlMs])

  return (
    <div
      className={cn(
        'pointer-events-auto flex w-80 items-start gap-2.5 rounded-menu border border-(--stroke-float) bg-surface-popover px-3 py-2.5 text-content-primary shadow-float backdrop-blur-md',
        leaving
          // Inert while it animates out: a card the user can still click is a
          // card whose action can fire twice, or fire as it disappears.
          ? 'pointer-events-none animate-toast-out'
          : toast.placement === 'top-center'
            ? 'animate-toast-in-top'
            : 'animate-toast-in-bottom',
      )}
      data-state={leaving ? 'closed' : 'open'}
      role={toast.kind === 'error' ? 'alert' : 'status'}
    >
      <Icon className={cn('mt-0.5 size-3.5 shrink-0', style.iconClass)} />
      <div className="min-w-0 flex-1">
        <div className="text-pb-caption font-semibold">{toast.title}</div>
        {toast.description ? <div className="pb-selectable mt-0.5 text-pb-micro text-content-secondary">{toast.description}</div> : null}
      </div>
      {toast.action ? (
        // Running the verb dismisses the card: the important stack never
        // auto-expires, so a toast left sitting after its action is done is
        // just litter the user has to clear by hand.
        <Button
          className="shrink-0"
          disabled={leaving}
          onClick={() => {
            // Belt and braces alongside pointer-events-none: keyboard and
            // programmatic activation must not slip through the exit window.
            if (leaving) return
            toast.action?.onClick()
            leave()
          }}
          size="xs"
          variant="text"
        >
          {toast.action.label}
        </Button>
      ) : null}
      <button
        aria-label="Dismiss notification"
        className="cursor-pointer rounded-iconbtn p-0.5 text-content-quaternary transition-colors duration-100 hover:bg-control-hover hover:text-content-secondary"
        onClick={leave}
        type="button"
      >
        <XGlyph className="size-3" />
      </button>
    </div>
  )
}

function ImportantStack({ dismiss, toasts }: { dismiss: (id: string) => void; toasts: ToastItem[] }) {
  const [expanded, setExpanded] = useState(false)
  // The IDS being cleared, not a sticky boolean: a flag left true would also
  // close every toast pushed during the 150ms exit window, so an error raised
  // while the stack drained vanished before it was ever read.
  const [clearingIds, setClearingIds] = useState<readonly string[]>([])

  useEffect(() => {
    if (toasts.length <= 1) setExpanded(false)
  }, [toasts.length])

  // Drop ids that have finished leaving, so the set empties itself.
  useEffect(() => {
    const live = new Set(toasts.map((toast) => toast.id))
    setClearingIds((current) => {
      const next = current.filter((id) => live.has(id))
      return next.length === current.length ? current : next
    })
  }, [toasts])

  const clearing = clearingIds.length > 0
  const visible = expanded || clearing ? toasts : toasts.slice(0, 1)
  const folded = toasts.length - visible.length

  return (
    // Anchored BELOW the frameless titlebar: the drag region is 34px tall, and
    // a toast painted over it both hides the title and eats the drag strip.
    <div className="pointer-events-none fixed inset-x-0 top-[calc(var(--pb-titlebar-height)+8px)] z-over-modal flex flex-col items-center gap-2">
      {visible.map((toast) => (
        <ToastCard closing={clearingIds.includes(toast.id)} key={toast.id} onDismiss={() => dismiss(toast.id)} toast={toast} />
      ))}
      {folded > 0 ? (
        <button
          className="pointer-events-auto cursor-pointer animate-toast-in-top rounded-menu border border-(--stroke-float) bg-surface-popover px-2.5 py-1 text-pb-micro font-medium text-content-secondary shadow-float backdrop-blur-md hover:bg-control-hover"
          onClick={() => setExpanded(true)}
          type="button"
        >
          {folded} more
        </button>
      ) : null}
      {toasts.length > 1 ? (
        <div className="pointer-events-auto flex items-center gap-1">
          {expanded ? (
            <button
              className="cursor-pointer rounded-menu px-2.5 py-1 text-pb-micro font-medium text-content-tertiary hover:bg-control-hover hover:text-content-primary"
              onClick={() => setExpanded(false)}
              type="button"
            >
              Collapse
            </button>
          ) : null}
          <button
            className="cursor-pointer rounded-menu px-2.5 py-1 text-pb-micro font-medium text-content-tertiary hover:bg-control-hover hover:text-content-primary"
            onClick={() => setClearingIds(toasts.map((toast) => toast.id))}
            type="button"
          >
            Clear all
          </button>
        </div>
      ) : null}
    </div>
  )
}

/** Mount ONCE, inside ToastProvider. Portals to <body> so the stacks float
 *  above every shell surface on the z-over-modal rung. */
export function ToastStacks() {
  const context = useContext(ToastContext)
  const [host, setHost] = useState<HTMLElement | null>(null)

  useEffect(() => {
    setHost(document.body)
  }, [])

  if (context === null || host === null) return null

  const important = context.toasts.filter((toast) => toast.placement === 'top-center')
  const ambient = context.toasts.filter((toast) => toast.placement === 'bottom-right')

  return createPortal(
    <>
      {important.length > 0 ? <ImportantStack dismiss={context.dismiss} toasts={important} /> : null}
      {ambient.length > 0 ? (
        <div className="pointer-events-none fixed bottom-10 right-4 z-over-modal flex flex-col items-end gap-2">
          {ambient.map((toast) => (
            <ToastCard key={toast.id} onDismiss={() => context.dismiss(toast.id)} toast={toast} ttlMs={AMBIENT_TTL_MS} />
          ))}
        </div>
      ) : null}
    </>,
    host,
  )
}
