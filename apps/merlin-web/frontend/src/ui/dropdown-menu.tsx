import * as DropdownMenuPrimitive from '@radix-ui/react-dropdown-menu'
import * as React from 'react'

import { cn } from './cn'
import { CheckGlyph, ChevronDownGlyph, ChevronRightGlyph } from './glyphs'
import { usePortalContainer } from './portal-container'
import { SectionLabel } from './section-label'

/**
 * Dropdown menus. 96%-opacity elevated surface + backdrop-blur, shadow-menu,
 * rounded-menu, zoom-in-95/fade-in entrance at 200ms. Kebab/menu triggers
 * never get a Tip — the affordance is "open menu"; verbs live in the items.
 */

const DropdownMenu = DropdownMenuPrimitive.Root
const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger
const DropdownMenuGroup = DropdownMenuPrimitive.Group
const DropdownMenuPortal = DropdownMenuPrimitive.Portal
const DropdownMenuSub = DropdownMenuPrimitive.Sub
const DropdownMenuRadioGroup = DropdownMenuPrimitive.RadioGroup

const contentClass = cn(
  'pb-portal pb-scroll z-modal-popover max-h-[min(60vh,24rem)] min-w-[10rem] overflow-y-auto rounded-menu border border-stroke-secondary bg-surface-popover p-1 text-content-primary shadow-menu backdrop-blur-md',
  // Grow from the trigger, and collapse back into it on close instead of
  // vanishing — the exit is what makes a menu feel attached to its button.
  'origin-(--radix-dropdown-menu-content-transform-origin)',
  'data-[state=open]:animate-zoom-in data-[state=closed]:animate-zoom-out',
)

function DropdownMenuContent({ className, sideOffset = 4, ...props }: React.ComponentProps<typeof DropdownMenuPrimitive.Content>) {
  // Inside a Dialog this portals into the dialog, so clicking our own menu is
  // not read as an outside interaction that closes the dialog. `null` keeps
  // Radix's document.body default everywhere else.
  const container = usePortalContainer()

  return (
    <DropdownMenuPrimitive.Portal container={container}>
      <DropdownMenuPrimitive.Content
        className={cn(contentClass, className)}
        data-slot="dropdown-menu-content"
        sideOffset={sideOffset}
        {...props}
      />
    </DropdownMenuPrimitive.Portal>
  )
}

const itemClass = cn(
  'relative flex cursor-pointer select-none items-center gap-2 rounded-[3px] px-2 py-1 text-pb-caption outline-none transition-colors duration-100',
  'focus:bg-surface-hover focus:text-content-primary data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
  '[&_svg]:size-3.5 [&_svg]:shrink-0 [&_svg]:text-content-tertiary',
  // Destructive verbs colour the label AND its glyph, and wash red on focus —
  // one treatment for "Delete…" in every menu.
  'data-[variant=destructive]:text-sev-red data-[variant=destructive]:focus:bg-sev-red-soft data-[variant=destructive]:focus:text-sev-red-text data-[variant=destructive]:[&_svg]:text-sev-red data-[variant=destructive]:focus:[&_svg]:text-sev-red-text',
)

export type DropdownMenuItemVariant = 'default' | 'destructive'

function DropdownMenuItem({
  className,
  inset,
  variant = 'default',
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Item> & { inset?: boolean; variant?: DropdownMenuItemVariant }) {
  return (
    <DropdownMenuPrimitive.Item
      className={cn(itemClass, inset && 'pl-7', className)}
      data-slot="dropdown-menu-item"
      data-variant={variant}
      {...props}
    />
  )
}

function DropdownMenuCheckboxItem({
  className,
  children,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.CheckboxItem>) {
  return (
    <DropdownMenuPrimitive.CheckboxItem className={cn(itemClass, 'pl-7', className)} data-slot="dropdown-menu-checkbox-item" {...props}>
      <span className="absolute left-2 flex size-3.5 items-center justify-center">
        <DropdownMenuPrimitive.ItemIndicator>
          <CheckGlyph className="size-3 text-accent" />
        </DropdownMenuPrimitive.ItemIndicator>
      </span>
      {children}
    </DropdownMenuPrimitive.CheckboxItem>
  )
}

function DropdownMenuRadioItem({ className, children, ...props }: React.ComponentProps<typeof DropdownMenuPrimitive.RadioItem>) {
  return (
    <DropdownMenuPrimitive.RadioItem className={cn(itemClass, 'pl-7', className)} data-slot="dropdown-menu-radio-item" {...props}>
      <span className="absolute left-2 flex size-3.5 items-center justify-center">
        <DropdownMenuPrimitive.ItemIndicator>
          <span className="size-1.5 rounded-full bg-accent" />
        </DropdownMenuPrimitive.ItemIndicator>
      </span>
      {children}
    </DropdownMenuPrimitive.RadioItem>
  )
}

/** Group heading inside a menu. Composes `SectionLabel` — a menu heading is an
 *  eyebrow like any other, and hand-rolling it is how it drifted to quaternary
 *  ink (2.64:1) and a different tracking from every other eyebrow. */
function DropdownMenuLabel({ children, className, ...props }: React.ComponentProps<typeof DropdownMenuPrimitive.Label>) {
  return (
    <DropdownMenuPrimitive.Label asChild data-slot="dropdown-menu-label" {...props}>
      <SectionLabel className={cn('px-2 pb-0.5 pt-1.5', className)}>{children}</SectionLabel>
    </DropdownMenuPrimitive.Label>
  )
}

function DropdownMenuSeparator({ className, ...props }: React.ComponentProps<typeof DropdownMenuPrimitive.Separator>) {
  return (
    <DropdownMenuPrimitive.Separator
      className={cn('-mx-1 my-1 h-px bg-stroke-tertiary', className)}
      data-slot="dropdown-menu-separator"
      {...props}
    />
  )
}

/** Trailing hotkey hint inside a menu item ("⌘N"). A shortcut the user is
 *  meant to read is readable text, not a 10px whisper. */
function DropdownMenuShortcut({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn('ml-auto pl-4 font-kbd text-pb-micro tracking-wide text-content-tertiary', className)}
      data-slot="dropdown-menu-shortcut"
      {...props}
    />
  )
}

function DropdownMenuSubTrigger({ className, children, ...props }: React.ComponentProps<typeof DropdownMenuPrimitive.SubTrigger>) {
  return (
    <DropdownMenuPrimitive.SubTrigger
      className={cn(itemClass, 'data-[state=open]:bg-surface-hover', className)}
      data-slot="dropdown-menu-sub-trigger"
      {...props}
    >
      {children}
      <ChevronRightGlyph className="ml-auto" />
    </DropdownMenuPrimitive.SubTrigger>
  )
}

function DropdownMenuSubContent({ className, ...props }: React.ComponentProps<typeof DropdownMenuPrimitive.SubContent>) {
  return <DropdownMenuPrimitive.SubContent className={cn(contentClass, className)} data-slot="dropdown-menu-sub-content" {...props} />
}

/** Trailing caret for a Button used as a DropdownMenuTrigger. A menu trigger
 *  without a caret reads as a label, not an affordance. */
function DropdownMenuTriggerCaret({ className, ...props }: React.ComponentProps<'svg'>) {
  return <ChevronDownGlyph className={cn('text-content-tertiary', className)} {...props} />
}

export {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuPortal,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
  DropdownMenuTriggerCaret,
}
