import type * as React from 'react'

/**
 * The sidebar item contract (plan contract C8).
 *
 * Feature areas describe their list rows with these fields and hand them to the
 * shell — `useChatSessions().sidebarSection(…)`, `useRecentScansSidebarSection()`.
 * They live in their own module so a feature can import the types without
 * pulling in the shell's React tree.
 *
 * Every field is optional except id/label: a nav destination is the same row
 * grammar as a chat or a scan, just with fewer parts filled in.
 */

/** One entry in a row's kebab / right-click menu. */
export interface SidebarMenuItem {
  label: string
  onSelect: () => void
  /** Irreversible verbs ("Delete…") — renders in sev-red. */
  destructive?: boolean
  /** Shortcut hint shown right-aligned in the menu. */
  shortcut?: string
}

export interface SidebarNavItem {
  id: string
  label: string
  icon?: React.ReactNode
  /** Marks the current route / selected record. */
  active?: boolean
  /** Trailing affordance (a count, a live dot) when `tone`/`meta` don't fit. */
  trailing?: React.ReactNode
  /** Right-aligned relative time ("3 min ago"). Hidden under the kebab on hover. */
  meta?: string
  /** Severity of `statusLabel`. */
  tone?: 'good' | 'bad' | 'warn' | 'info' | 'muted'
  /** Short status word rendered with a dot ("Failed") — never colour alone. */
  statusLabel?: string
  /** Row actions: hover/focus kebab AND right-click, same list. */
  menuItems?: SidebarMenuItem[]
  onClick?: () => void
  /** Enables inline rename (double-click / F2). Receives the trimmed title. */
  onRename?: (title: string) => void
}

export interface SidebarSection {
  id: string
  /** Small-caps section label. Omit for the unlabeled lead nav group. */
  label?: string
  items: SidebarNavItem[]
  /**
   * One quiet line to render INSTEAD of rows when `items` is empty ("No chats
   * yet"). Without it an empty section is skipped entirely — a heading with
   * nothing under it reads as a broken group.
   */
  emptyLabel?: string
}

/**
 * Footer state — a READOUT, never an action.
 *
 * The recovery verbs live on the offline banner alone, and the shell hides this
 * line entirely whenever the banner is up: the audit found one engine state
 * announced in three places, in two different phrasings, with the same verb
 * rendered once as a button and once as an underlined link.
 */
export interface SidebarStatus {
  tone: 'good' | 'bad' | 'warn' | 'muted'
  label: string
}
