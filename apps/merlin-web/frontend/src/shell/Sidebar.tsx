import { KbdGroup, PlusGlyph, StatusDot, cn } from '@/ui'
import { SidebarRow } from './SidebarRow'
import { SHELL_COPY } from './copy'
import type { SidebarNavItem, SidebarSection, SidebarStatus } from './sidebar-types'

export interface SidebarProps {
  sections: SidebarSection[]
  /** The first row — "New chat". A ROW, not a filled button: it is a frequent
   *  action, not the loudest thing on the screen. */
  primaryAction?: { label: string; onClick: () => void; shortcut?: readonly string[] }
  /** Footer. Passed ONLY when the engine needs attention — a permanent "Ready"
   *  line here would be the second copy of what the statusbar already says. */
  status?: SidebarStatus
  className?: string
}

/**
 * The sidebar. Flat sections on the sidebar surface: a small-caps label, flush
 * 13px rows, hover wash, selected = `bg-accent-soft` + accent tick. Sections
 * double as the nav tree AND the list areas — a chat list is just a section
 * whose items carry `meta`, `menuItems` and `onRename`.
 *
 * A section with no rows renders its `emptyLabel` or is skipped entirely;
 * neither list area may leave a heading standing over nothing.
 *
 * The aside is always mounted — AppShell animates the WRAPPER's width, so a
 * collapse slides instead of snapping and the rows never reflow on the way out.
 *
 * The footer is a readout with no action, and the shell passes it only when the
 * offline banner is NOT up — one state, at most two places, one wording.
 */
export function Sidebar({ className, primaryAction, sections, status }: SidebarProps) {
  const visibleSections = sections.filter((section) => section.items.length > 0 || section.emptyLabel !== undefined)

  // "New chat" is a ROW, so it goes through the row component like every other
  // row — same height, same hover, same truncation. Duplicating the markup here
  // is how the two drift apart.
  //
  // Built inline rather than memoised: callers pass a fresh `primaryAction`
  // object every render, so a `useMemo` keyed on it would recompute anyway
  // while costing a dependency check and reading as a guarantee it isn't.
  const primaryItem: SidebarNavItem | null =
    primaryAction === undefined
      ? null
      : {
        id: 'sidebar-primary-action',
        label: primaryAction.label,
        icon: <PlusGlyph />,
        onClick: primaryAction.onClick,
        ...(primaryAction.shortcut !== undefined
          ? { trailing: <KbdGroup keys={primaryAction.shortcut} size="sm" /> }
          : {}),
      }

  return (
    <aside
      className={cn('flex h-full w-full flex-col overflow-hidden border-r border-stroke-tertiary bg-surface-sidebar', className)}
      data-slot="sidebar"
    >
      {primaryItem !== null ? (
        <div className="px-2 pb-1 pt-2">
          <SidebarRow item={primaryItem} />
        </div>
      ) : null}

      <nav
        aria-label={SHELL_COPY.sidebarNavLabel}
        className="pb-scroll-hover min-h-0 flex-1 overflow-y-auto px-2 py-2"
      >
        {visibleSections.map((section) => (
          <div className="pb-3" key={section.id}>
            {section.label !== undefined ? (
              <div className="px-2 pb-1 pt-1 text-pb-micro font-semibold uppercase tracking-[0.08em] text-content-tertiary">
                {section.label}
              </div>
            ) : null}
            {section.items.length === 0 ? (
              <div className="px-2 py-1 text-pb-caption text-content-tertiary">{section.emptyLabel}</div>
            ) : (
              section.items.map((item) => <SidebarRow item={item} key={item.id} />)
            )}
          </div>
        ))}
      </nav>

      {status !== undefined ? (
        <div className="flex items-center gap-2 px-3 py-2.5" data-slot="sidebar-status">
          <StatusDot className="min-w-0" label={status.label} pulse={status.tone === 'warn'} tone={status.tone} />
        </div>
      ) : null}
    </aside>
  )
}
