import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
  Input,
  OverflowTip,
  PanelRowKebab,
  StatusDot,
  cn,
} from '@/ui'
import type { SidebarNavItem } from './sidebar-types'

/**
 * What the row's decorations cost, in px, so the title's share can be derived
 * from the row width ALONE. Deriving it from the title's own measured width
 * would oscillate: hiding `meta` widens the title, which un-hides `meta`.
 *
 * Measured on a live 220px recent-scan row (W3-1 defect D2): icon 16 + gaps 24
 * + padding 16 + kebab reserve 28 = 84 fixed; "Issues found" 78.5; "13 h ago"
 * 51.3. Text widths are estimated per character at the 11px UI face rather than
 * measured, for the same anti-oscillation reason.
 */
const ROW_FIXED_COST = 84
const STATUS_DOT_COST = 14
const CHAR_PX = 6.2
/** Below this the title stops being a title and starts being an abbreviation. */
const MIN_TITLE_PX = 60
/** Hard floor from the Wave-4 ruling: never a status WORD on a row this narrow. */
const COMPACT_ROW_PX = 200

/**
 * One sidebar row — nav destination, chat, or scan. They are the same grammar
 * on purpose: icon, label, optional meta/status, and the actions that belong to
 * THAT row rather than to a header kebab three rows away.
 *
 * Structure note: the row body is a real `<button>`, so the whole row is
 * focusable and Enter-activatable. The kebab is a SIBLING positioned over the
 * row's right edge — nesting a button inside a button is invalid HTML and the
 * inner one stops being reachable by keyboard.
 *
 * Rename is inline (double-click or F2): a text field in the row's own place,
 * Enter commits, Esc cancels, blur commits. A dialog for renaming a chat is
 * three interactions where one will do.
 */
export function SidebarRow({ item }: { item: SidebarNavItem }) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState(item.label)
  const inputRef = useRef<HTMLInputElement>(null)
  const rowRef = useRef<HTMLDivElement>(null)
  const [rowWidth, setRowWidth] = useState(0)

  useLayoutEffect(() => {
    const node = rowRef.current
    if (node === null || typeof ResizeObserver === 'undefined') return
    setRowWidth(node.clientWidth)
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width
      if (width !== undefined) setRowWidth(width)
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  const menuItems = item.menuItems ?? []
  const hasMenu = menuItems.length > 0
  const canRename = item.onRename !== undefined
  const active = item.active === true

  // The title WINS the row. Decorations drop in priority order — the relative
  // time first, then the status word (the dot keeps the meaning) — until the
  // title clears MIN_TITLE_PX. Before this, a 220px row gave the title 14px and
  // rendered "S..", sacrificing the only part of the row that identifies it.
  const statusCost = item.statusLabel === undefined ? 0 : item.statusLabel.length * CHAR_PX + STATUS_DOT_COST
  const metaCost = item.meta === undefined ? 0 : item.meta.length * CHAR_PX
  // Width 0 = not measured yet (first paint, or no ResizeObserver): show
  // everything rather than flashing a stripped row.
  const measured = rowWidth > 0
  const titleWith = (cost: number) => rowWidth - ROW_FIXED_COST - cost
  const showMeta = !measured || titleWith(statusCost + metaCost) >= MIN_TITLE_PX
  const showStatusLabel =
    item.statusLabel !== undefined
    && (!measured
      || (rowWidth >= COMPACT_ROW_PX && titleWith(statusCost + (showMeta ? metaCost : 0)) >= MIN_TITLE_PX))

  useEffect(() => {
    if (renaming) inputRef.current?.select()
  }, [renaming])

  const startRename = () => {
    if (!canRename) return
    setDraft(item.label)
    setRenaming(true)
  }

  const commitRename = () => {
    if (!renaming) return
    setRenaming(false)
    const next = draft.trim()
    if (next !== '' && next !== item.label) item.onRename?.(next)
  }

  if (renaming) {
    return (
      <div className="px-1 py-0.5" data-slot="sidebar-row-rename">
        <Input
          onBlur={commitRename}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              commitRename()
            } else if (event.key === 'Escape') {
              event.preventDefault()
              event.stopPropagation()
              setRenaming(false)
            }
          }}
          ref={inputRef}
          size="sm"
          value={draft}
          /* The row was just turned into a field by an explicit rename
             gesture — focus belongs here, and nowhere else moved it. */
          autoFocus
        />
      </div>
    )
  }

  return (
    <div className="group/row relative flex items-center" data-slot="sidebar-row" ref={rowRef}>
      <button
        aria-current={active ? 'page' : undefined}
        className={cn(
          'relative flex w-full min-w-0 cursor-pointer select-none items-center gap-2 rounded-iconbtn px-2 py-1.5 text-left text-pb-body leading-5 transition-colors duration-100',
          '[&_svg]:size-4 [&_svg]:shrink-0',
          active
            ? 'bg-accent-soft font-medium text-content-primary'
            : 'text-content-secondary hover:bg-surface-hover hover:text-content-primary',
          hasMenu && 'pr-7',
        )}
        onClick={item.onClick}
        onContextMenu={
          hasMenu
            ? (event) => {
              event.preventDefault()
              setMenuOpen(true)
            }
            : undefined
        }
        onDoubleClick={canRename ? startRename : undefined}
        onKeyDown={(event) => {
          if (canRename && event.key === 'F2') {
            event.preventDefault()
            startRename()
          }
        }}
        type="button"
      >
        {active ? <span className="absolute left-0 top-1/2 h-3 w-0.5 -translate-y-1/2 rounded-full bg-accent" /> : null}
        {item.icon !== undefined ? <span className="text-content-tertiary">{item.icon}</span> : null}
        <OverflowTip label={item.label}>
          <span className="min-w-0 flex-1 truncate">{item.label}</span>
        </OverflowTip>
        {item.statusLabel !== undefined ? (
          <StatusDot
            aria-label={showStatusLabel ? undefined : item.statusLabel}
            className="shrink-0"
            tone={item.tone ?? 'muted'}
            {...(showStatusLabel ? { label: item.statusLabel } : {})}
          />
        ) : null}
        {item.meta !== undefined && showMeta ? (
          <span className={cn('shrink-0 text-pb-micro text-content-tertiary', hasMenu && 'group-hover/row:opacity-0')}>
            {item.meta}
          </span>
        ) : null}
        {item.trailing !== undefined ? <span className="shrink-0">{item.trailing}</span> : null}
      </button>

      {hasMenu ? (
        <DropdownMenu onOpenChange={setMenuOpen} open={menuOpen}>
          <DropdownMenuTrigger asChild>
            <PanelRowKebab
              className={cn(
                'absolute right-1 opacity-0 transition-opacity duration-100',
                'group-hover/row:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100',
              )}
            />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-40">
            {menuItems.map((menuItem) => (
              <DropdownMenuItem
                key={menuItem.label}
                onSelect={menuItem.onSelect}
                variant={menuItem.destructive === true ? 'destructive' : 'default'}
              >
                {menuItem.label}
                {menuItem.shortcut !== undefined ? <DropdownMenuShortcut>{menuItem.shortcut}</DropdownMenuShortcut> : null}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}
    </div>
  )
}
