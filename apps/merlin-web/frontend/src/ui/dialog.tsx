import * as DialogPrimitive from '@radix-ui/react-dialog'
import * as React from 'react'

import { Button } from './button'
import { cn } from './cn'
import { XGlyph } from './glyphs'
import { PortalContainerProvider } from './portal-container'

/**
 * Dialogs. Centered card, max-w-lg, rounded-dialog, borderless elevation
 * (shadow-float + the --stroke-float currentColor hairline), overlay
 * bg-black/22 with a light backdrop blur. Entrance: 200ms fade (overlay) +
 * zoom-in-95 (card). Esc closes; the close affordance is an x-icon, never the
 * word "Close".
 */

const Dialog = DialogPrimitive.Root
const DialogTrigger = DialogPrimitive.Trigger
const DialogPortal = DialogPrimitive.Portal
const DialogClose = DialogPrimitive.Close

function DialogOverlay({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      className={cn(
        'pb-portal fixed inset-0 z-modal-backdrop bg-black/22 backdrop-blur-[2px]',
        'data-[state=open]:animate-fade-in data-[state=closed]:animate-fade-out',
        className,
      )}
      data-slot="dialog-overlay"
      {...props}
    />
  )
}

export interface DialogContentProps extends React.ComponentProps<typeof DialogPrimitive.Content> {
  /**
   * Size to the content instead of capping at 85vh with an inner scroll. For
   * short dialogs whose body must not scroll (a two-line confirmation, a
   * picker that manages its own scrolling).
   */
  fitContent?: boolean
}

/**
 * Dialog shell + scrolling body. The shell owns position, width, skin and an
 * 85vh ceiling; the body scrolls inside it. A tall dialog therefore keeps its
 * close affordance reachable instead of running off the window — call sites
 * must not add their own `max-h`/`overflow` (the model picker used to). Pass
 * `fitContent` to opt out of the ceiling entirely.
 *
 * The content node is published through `PortalContainerProvider`, so menus,
 * selects and tooltips opened inside the dialog portal INTO the dialog rather
 * than to `document.body` — otherwise Radix reads a click on your own menu as
 * an outside interaction and closes the dialog under you.
 */
const DialogContent = React.forwardRef<HTMLDivElement, DialogContentProps>(
  ({ className, children, fitContent = false, ...props }, forwardedRef) => {
    const [content, setContent] = React.useState<HTMLDivElement | null>(null)

    const setRefs = React.useCallback(
      (node: HTMLDivElement | null) => {
        setContent(node)
        if (typeof forwardedRef === 'function') forwardedRef(node)
        else if (forwardedRef !== null && forwardedRef !== undefined) forwardedRef.current = node
      },
      [forwardedRef],
    )

    return (
      <DialogPortal>
        <DialogOverlay />
        <DialogPrimitive.Content
          className={cn(
            'pb-portal fixed left-1/2 top-1/2 z-modal flex w-full max-w-lg -translate-x-1/2 -translate-y-1/2 flex-col rounded-dialog border border-(--stroke-float) bg-surface-elevated text-content-primary shadow-float',
            fitContent ? undefined : 'max-h-[85vh]',
            'data-[state=open]:animate-zoom-in data-[state=closed]:animate-zoom-out',
            className,
          )}
          data-slot="dialog-content"
          ref={setRefs}
          {...props}
        >
          <PortalContainerProvider container={content}>
            <div
              className={cn('flex flex-col gap-3 p-5', fitContent ? undefined : 'pb-scroll min-h-0 flex-1 overflow-y-auto')}
              data-slot="dialog-body"
            >
              {children}
            </div>
            <DialogPrimitive.Close asChild>
              <Button aria-label="Close" className="absolute right-3 top-3" size="icon-xs" variant="ghost">
                <XGlyph />
              </Button>
            </DialogPrimitive.Close>
          </PortalContainerProvider>
        </DialogPrimitive.Content>
      </DialogPortal>
    )
  },
)
DialogContent.displayName = 'DialogContent'

function DialogHeader({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('flex flex-col gap-1 pr-8 text-left', className)} data-slot="dialog-header" {...props} />
}

function DialogFooter({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('flex items-center justify-end gap-2 pt-2', className)} data-slot="dialog-footer" {...props} />
}

function DialogTitle({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn('text-pb-section font-semibold text-content-primary', className)}
      data-slot="dialog-title"
      {...props}
    />
  )
}

function DialogDescription({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      className={cn('pb-selectable text-pb-caption text-content-secondary', className)}
      data-slot="dialog-description"
      {...props}
    />
  )
}

export { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogPortal, DialogTitle, DialogTrigger }
