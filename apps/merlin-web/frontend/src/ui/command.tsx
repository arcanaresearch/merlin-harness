import { Command as CommandPrimitive } from 'cmdk'
import * as React from 'react'

import { cn } from './cn'
import { SearchGlyph } from './glyphs'
import { SectionLabel } from './section-label'

/**
 * Keyboard-first command surface (cmdk). Powers the ⌘K palette and any
 * type-to-filter picker.
 *
 * The contract: the INPUT keeps focus, arrows move the active row, Enter
 * commits it. Every row is reachable without touching the mouse — that is the
 * whole point of the surface, and the hand-rolled listbox it replaces had none
 * of it.
 */

/**
 * True while the pointer must be treated as absent.
 *
 * cmdk selects on pointer-enter. A list that opens under a parked cursor — or
 * re-flows under one as the query narrows — hands the selection to whatever row
 * slid beneath the mouse, and Enter commits THAT. Nothing about that came from
 * the user, so the pointer stays inert until it actually MOVES (or scrolls,
 * which is also a hand on the mouse). One real movement hands hover back for
 * the rest of the surface's life.
 */
export function usePointerQuiet(): boolean {
  const [quiet, setQuiet] = React.useState(true)

  React.useEffect(() => {
    const wake = () => setQuiet(false)
    const options = { capture: true } as const

    window.addEventListener('mousemove', wake, options)
    window.addEventListener('wheel', wake, options)

    return () => {
      window.removeEventListener('mousemove', wake, options)
      window.removeEventListener('wheel', wake, options)
    }
  }, [])

  return quiet
}

function Command({ className, ...props }: React.ComponentProps<typeof CommandPrimitive>) {
  return (
    <CommandPrimitive
      className={cn('flex h-full w-full min-w-0 flex-col overflow-hidden text-content-primary', className)}
      data-slot="command"
      {...props}
    />
  )
}

export type CommandInputProps = React.ComponentProps<typeof CommandPrimitive.Input>

function CommandInput({ className, ...props }: CommandInputProps) {
  return (
    <div className="flex items-center gap-2 border-b border-stroke-tertiary px-3 py-2.5" data-slot="command-input-wrapper">
      <SearchGlyph className="size-3.5 shrink-0 text-content-quaternary" />
      <CommandPrimitive.Input
        className={cn(
          'w-full min-w-0 bg-transparent text-pb-body text-content-primary outline-none placeholder:text-content-tertiary',
          className,
        )}
        data-slot="command-input"
        {...props}
      />
    </div>
  )
}

function CommandList({ className, ...props }: React.ComponentProps<typeof CommandPrimitive.List>) {
  const pointerQuiet = usePointerQuiet()

  return (
    <CommandPrimitive.List
      className={cn('pb-scroll max-h-80 overflow-y-auto overflow-x-hidden p-1', pointerQuiet && 'pointer-events-none', className)}
      data-slot="command-list"
      {...props}
    />
  )
}

function CommandEmpty({ className, ...props }: React.ComponentProps<typeof CommandPrimitive.Empty>) {
  return (
    <CommandPrimitive.Empty
      className={cn('px-3 py-8 text-center text-pb-caption text-content-tertiary', className)}
      data-slot="command-empty"
      {...props}
    />
  )
}

/**
 * Group of command rows. The heading is wrapped in `SectionLabel` rather than
 * styled through `[cmdk-group-heading]` utilities — same eyebrow as every other
 * block label, so it cannot drift again. cmdk still owns the wrapper element
 * (it needs it for filtering), so the wrapper keeps only its padding.
 */
function CommandGroup({ className, heading, ...props }: React.ComponentProps<typeof CommandPrimitive.Group>) {
  return (
    <CommandPrimitive.Group
      className={cn('overflow-hidden', '**:[[cmdk-group-heading]]:px-2 **:[[cmdk-group-heading]]:pt-2', className)}
      data-slot="command-group"
      heading={heading === undefined || heading === null ? heading : <SectionLabel>{heading}</SectionLabel>}
      {...props}
    />
  )
}

function CommandSeparator({ className, ...props }: React.ComponentProps<typeof CommandPrimitive.Separator>) {
  return (
    <CommandPrimitive.Separator className={cn('-mx-1 my-1 h-px bg-stroke-tertiary', className)} data-slot="command-separator" {...props} />
  )
}

function CommandItem({ className, ...props }: React.ComponentProps<typeof CommandPrimitive.Item>) {
  return (
    <CommandPrimitive.Item
      className={cn(
        'relative flex cursor-pointer select-none items-center gap-2 rounded-[3px] px-2 py-1.5 text-pb-caption outline-none transition-colors duration-100',
        'data-[selected=true]:bg-surface-hover data-[selected=true]:text-content-primary',
        'data-[disabled=true]:pointer-events-none data-[disabled=true]:opacity-50',
        '[&_svg]:size-3.5 [&_svg]:shrink-0 [&_svg]:text-content-tertiary',
        className,
      )}
      data-slot="command-item"
      {...props}
    />
  )
}

/** Trailing shortcut hint on a row. Use `<KbdGroup>` inside for real chips. */
function CommandShortcut({ className, ...props }: React.ComponentProps<'span'>) {
  return <span className={cn('ml-auto flex shrink-0 items-center gap-1 pl-4', className)} data-slot="command-shortcut" {...props} />
}

export { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandSeparator, CommandShortcut }
