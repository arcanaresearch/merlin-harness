import * as React from 'react'
import { useEffect, useState } from 'react'

import { getVersions } from '@/lib/desktop'
import { StatusDot, cn } from '@/ui'
import { SHELL_COPY } from './copy'
import { STATUSBAR_HEIGHT } from './layout-constants'

export interface StatusbarConnection {
  tone: 'good' | 'bad' | 'warn' | 'muted'
  label: string
  /** The engine's own short reason for an unhealthy state, shown after the
   *  label in tertiary ink. Omit when the label already says everything. */
  detail?: string
  /** Where the state can be acted on (Settings › Advanced). */
  onSelect?: () => void
}

export interface StatusbarProps {
  /** Left cluster: the scanner-engine state. The ONE place it is reported. */
  connection?: StatusbarConnection
  /** Left cluster: "2 scans running", only while something is running. */
  activity?: { label: string; onSelect?: () => void }
  /** Right cluster: the version pill opens Settings › About. */
  onOpenAbout?: () => void
  className?: string
}

const ITEM_CLASS =
  'inline-flex h-full max-w-full items-center gap-1.5 px-1.5 text-pb-micro text-content-secondary transition-colors duration-100'
const ACTION_CLASS = 'cursor-pointer hover:bg-control-hover hover:text-content-primary'

/** A statusbar cell: a button when it leads somewhere, plain text when it doesn't. */
function StatusbarItem({
  ariaLabel,
  children,
  onSelect,
}: {
  /** Spells out where the cell leads when its text alone is a version number. */
  ariaLabel?: string
  children: React.ReactNode
  onSelect?: (() => void) | undefined
}) {
  if (onSelect === undefined) {
    return <span className={ITEM_CLASS}>{children}</span>
  }
  return (
    <button aria-label={ariaLabel} className={cn(ITEM_CLASS, ACTION_CLASS)} onClick={onSelect} type="button">
      {children}
    </button>
  )
}

/**
 * The 22px statusbar — the app's quiet permanent readout, on `bg-surface-bar`
 * so it steps away from the page instead of sharing its exact fill.
 *
 * Every cell is a real control that leads to where the thing can be changed:
 * the engine state opens Settings › Advanced, the version opens About. It used
 * to be 10px dead text saying "Ready · Paired · v0.1.0" — the smallest type in
 * the app, at 3.5:1, and nothing to click when the word was bad news.
 */
export function Statusbar({ activity, className, connection, onOpenAbout }: StatusbarProps) {
  const [version, setVersion] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    getVersions()
      .then((versions) => {
        if (!cancelled && versions !== null) setVersion(versions.app)
      })
      .catch(() => {
        // No bridge (browser sandbox) or the shell refused: the version cell
        // simply does not render. Nothing a person can act on.
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <footer
      className={cn(
        'flex w-full shrink-0 select-none items-center justify-between gap-2 border-t border-stroke-tertiary bg-surface-bar px-1.5 leading-none',
        className,
      )}
      data-slot="statusbar"
      style={{ height: STATUSBAR_HEIGHT }}
    >
      <div className="flex min-w-0 items-center">
        {connection !== undefined ? (
          <StatusbarItem onSelect={connection.onSelect}>
            <StatusDot pulse={connection.tone === 'warn'} tone={connection.tone} />
            <span className="truncate">{connection.label}</span>
            {connection.detail !== undefined ? (
              <span className="truncate text-content-tertiary">· {connection.detail}</span>
            ) : null}
          </StatusbarItem>
        ) : null}
        {activity !== undefined ? (
          <StatusbarItem onSelect={activity.onSelect}>
            <span className="truncate">{activity.label}</span>
          </StatusbarItem>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center">
        {version !== null ? (
          <StatusbarItem ariaLabel={SHELL_COPY.aboutItemLabel} onSelect={onOpenAbout}>
            <span>v{version}</span>
          </StatusbarItem>
        ) : null}
      </div>
    </footer>
  )
}
