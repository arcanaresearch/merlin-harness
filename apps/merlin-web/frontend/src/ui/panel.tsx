import * as React from 'react'

import { cn } from './cn'
import { CopyButton } from './copy-button'
import { MoreHorizontalGlyph } from './glyphs'

/**
 * Panel family — the structural unit for in-page content. FLAT, not boxed:
 * a panel is a section header + body separated by ONE hairline; structure
 * inside comes from whitespace and hairlines, never nested rounded boxes
 * (no card-in-card).
 *
 *   <Panel>
 *     <PanelHeader title="Findings" actions={…} />
 *     <PanelBody>…</PanelBody>
 *   </Panel>
 *
 *   <Panel>                                  // list variant
 *     <PanelHeader title="Queue" />
 *     <PanelList>
 *       <PanelListRow actions={<Kebab…/>}>…</PanelListRow>
 *     </PanelList>
 *   </Panel>
 */

export function Panel({ className, ...props }: React.ComponentProps<'section'>) {
  return <section className={cn('flex min-w-0 flex-col', className)} data-slot="panel" {...props} />
}

export interface PanelHeaderProps extends Omit<React.ComponentProps<'header'>, 'title'> {
  title: React.ReactNode
  /** Optional secondary line under the title. */
  description?: React.ReactNode
  /** Right-aligned header actions (Buttons, SegmentedControl…). */
  actions?: React.ReactNode
}

export function PanelHeader({ actions, className, description, title, ...props }: PanelHeaderProps) {
  return (
    <header className={cn('flex items-center justify-between gap-3 pb-2', className)} data-slot="panel-header" {...props}>
      <div className="min-w-0">
        <h2 className="truncate text-pb-section font-semibold text-content-primary">{title}</h2>
        {description ? <p className="mt-0.5 text-pb-caption text-content-tertiary">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  )
}

export function PanelBody({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('min-w-0 border-t border-stroke-tertiary pt-3', className)} data-slot="panel-body" {...props} />
}

/** Vertical row list. Rows are flush-left, separated by a single tertiary
 *  hairline — spacing does the grouping, never boxed rows. */
export function PanelList({ className, ...props }: React.ComponentProps<'div'>) {
  return <div className={cn('min-w-0 divide-y divide-stroke-tertiary border-t border-stroke-tertiary', className)} data-slot="panel-list" {...props} />
}

export interface PanelListRowProps extends React.ComponentProps<'div'> {
  /** Row action revealed on hover/focus — typically a kebab DropdownMenu. */
  actions?: React.ReactNode
  /** Makes the row interactive. The row's main area then renders as a real
   *  `<button type="button">`: focusable, Enter/Space activated, hover wash. */
  onClick?: () => void
  /**
   * This row is the current selection in a master/detail list.
   *
   * Lands `aria-current="true"` on the FOCUSABLE element (the row button), not
   * the wrapper — `...props` spreads onto the outer div, so a raw `aria-current`
   * passed by a screen never reaches the control a reader announces, leaving
   * selection colour-only. `data-selected` goes on the outer div for styling.
   */
  selected?: boolean
}

export function PanelListRow({ actions, children, className, onClick, selected = false, ...props }: PanelListRowProps) {
  const interactive = onClick !== undefined

  return (
    <div
      className={cn(
        'group/row flex min-w-0 items-center gap-3 transition-colors duration-100',
        // An interactive row's padding lives on the <button>, so the whole row
        // — edge to edge — is the click target rather than a padded gutter that
        // swallows clicks. A static row keeps the padding itself.
        interactive
          ? 'cursor-pointer pr-1 hover:bg-surface-hover has-[[data-slot=row-button]:focus-visible]:bg-surface-hover'
          : 'px-1 py-2',
        className,
      )}
      data-selected={selected || undefined}
      data-slot="panel-list-row"
      {...props}
    >
      {interactive ? (
        // A real button so the row is reachable by Tab and activated by
        // Enter/Space. Actions stay OUTSIDE it — nesting them would make them
        // unreachable and would nest interactive content.
        <button
          aria-current={selected ? true : undefined}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-3 py-2 pl-1 text-left outline-none"
          data-slot="row-button"
          onClick={onClick}
          type="button"
        >
          {children}
        </button>
      ) : (
        // A static row has no control to carry the state, so it lands here —
        // better on the wrapper than nowhere.
        <div aria-current={selected ? true : undefined} className="flex min-w-0 flex-1 items-center gap-3">
          {children}
        </div>
      )}
      {actions ? (
        <div className="shrink-0 opacity-0 transition-opacity duration-100 focus-within:opacity-100 group-focus-within/row:opacity-100 group-hover/row:opacity-100">
          {actions}
        </div>
      ) : null}
    </div>
  )
}

/** Default hover kebab trigger for PanelListRow actions. Menus on it carry the
 *  verbs; the kebab itself never gets a tooltip. Forwards its ref so it can be
 *  a `<DropdownMenuTrigger asChild>` child. */
export const PanelRowKebab = React.forwardRef<HTMLButtonElement, React.ComponentProps<'button'>>(
  ({ className, ...props }, ref) => (
    <button
      aria-label="Row actions"
      className={cn(
        'flex size-6 cursor-pointer items-center justify-center rounded-iconbtn text-content-tertiary transition-colors duration-100 hover:bg-control-hover hover:text-content-primary',
        className,
      )}
      data-slot="panel-row-kebab"
      ref={ref}
      type="button"
      {...props}
    >
      <MoreHorizontalGlyph className="size-3.5" />
    </button>
  ),
)
PanelRowKebab.displayName = 'PanelRowKebab'

export interface PanelDetailProps extends React.ComponentProps<'div'> {
  label: React.ReactNode
  value: React.ReactNode
  /**
   * The value is worth copying (an id, a hash, a path). Renders a `CopyButton`
   * after it. Pass `copyValue` when the copied text differs from what is shown
   * — a truncated display should still copy the full string.
   */
  copyable?: boolean
  /** Exact text to place on the clipboard; defaults to `value` when it is a string. */
  copyValue?: string
}

/** Read-only detail block inside a panel body — a labelled value, flush-left,
 *  hairline-separated from siblings when stacked. */
export function PanelDetail({ className, copyable = false, copyValue, label, value, ...props }: PanelDetailProps) {
  const clipboardText = copyValue ?? (typeof value === 'string' ? value : undefined)

  return (
    <div className={cn('flex items-baseline justify-between gap-4 py-1.5', className)} data-slot="panel-detail" {...props}>
      <span className="shrink-0 text-pb-caption text-content-tertiary">{label}</span>
      <div className="flex min-w-0 items-center justify-end gap-1">
        <span className={cn('min-w-0 truncate text-right text-pb-caption font-medium text-content-primary', copyable && 'pb-selectable')}>
          {value}
        </span>
        {copyable && clipboardText !== undefined ? (
          <CopyButton className="shrink-0" label="Copy value" value={clipboardText} />
        ) : null}
      </div>
    </div>
  )
}

/**
 * Empty state for a panel-local sub-list ONLY — an inner list inside a page
 * that already has its own content (a detail tab's outputs list, a nested
 * table). One quiet line, glyph opt-in.
 *
 * NOT for a page body or a screen's primary list: those use `EmptyState`, which
 * carries a real title, a per-screen glyph and the action. If this is the only
 * thing on the screen, you want `EmptyState`.
 */
export function PanelEmpty({ action, className, icon, title }: { action?: React.ReactNode; className?: string; icon?: React.ReactNode; title: React.ReactNode }) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-1 px-4 py-8 text-center', className)} data-slot="panel-empty">
      {icon ? <div className="text-content-quaternary [&_svg]:size-4">{icon}</div> : null}
      <div className="text-pb-caption text-content-tertiary">{title}</div>
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  )
}

const PILL_TONES = {
  good: 'bg-sev-green-soft text-sev-green-text',
  bad: 'bg-sev-red-soft text-sev-red-text',
  warn: 'bg-sev-yellow-soft text-sev-yellow-text',
  muted: 'bg-surface-tertiary text-content-tertiary',
  info: 'bg-sev-blue-soft text-sev-blue-text',
} as const

export interface PanelPillProps extends React.ComponentProps<'span'> {
  tone?: keyof typeof PILL_TONES
}

/** Status pill for findings severity, scan state, etc. Soft severity fill +
 *  severity text; `muted` for neutral counts. Title Case labels, never an
 *  engineering enum. Forwards its ref so it can be a `<Tip>` child. */
export const PanelPill = React.forwardRef<HTMLSpanElement, PanelPillProps>(({ className, tone = 'muted', ...props }, ref) => (
  <span
    className={cn(
      'inline-flex select-none items-center gap-1 rounded-full px-1.5 py-px text-pb-micro font-semibold',
      PILL_TONES[tone],
      className,
    )}
    data-slot="panel-pill"
    data-tone={tone}
    ref={ref}
    {...props}
  />
))
PanelPill.displayName = 'PanelPill'
