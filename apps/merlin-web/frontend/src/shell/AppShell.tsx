import * as React from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { isDesktop, onMenuAction } from '@/lib/desktop'
import { cn } from '@/ui'
import { Sidebar, type SidebarProps } from './Sidebar'
import { Statusbar, type StatusbarProps } from './Statusbar'
import { Titlebar, type TitlebarProps } from './Titlebar'
import { SIDEBAR_MAX_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_WIDTH } from './layout-constants'

export interface AppShellProps {
  /** Sidebar config (sections, primary action, footer status). */
  sidebar: Omit<SidebarProps, 'className'>
  /** Statusbar config. */
  statusbar?: Omit<StatusbarProps, 'className'>
  /** Titlebar config. Sidebar toggle + visibility are wired by the shell. */
  titlebar?: Omit<TitlebarProps, 'onToggleSidebar' | 'sidebarVisible'>
  /** New chat — the ⌘N chord and the sidebar's first row. */
  onNewChat?: () => void
  /** New scan — the ⇧⌘N chord. */
  onNewScan?: () => void
  /**
   * False while a modal task owns the window (first-run onboarding). The chords
   * would otherwise open the palette and Settings UNDERNEATH an opaque wizard,
   * and the palette's autofocus would steal typing into an invisible field.
   */
  chordsEnabled?: boolean
  /** Above the routed content — the engine offline banner. */
  banner?: React.ReactNode
  children: React.ReactNode
}

/**
 * Shell affordances a CHILD of the shell may drive — today just the sidebar,
 * whose visibility and width the shell owns (persisted, animated, resizable).
 * The command palette needs it for its "Toggle sidebar" action; without this it
 * would have to reach up past its own parent.
 */
export interface ShellActions {
  toggleSidebar: () => void
  sidebarVisible: boolean
}

const ShellActionsContext = React.createContext<ShellActions>({
  toggleSidebar: () => {},
  sidebarVisible: true,
})

/** Safe outside an AppShell (toggling is then a no-op). */
export function useShellActions(): ShellActions {
  return React.useContext(ShellActionsContext)
}

const VISIBLE_KEY = 'pirateboys.sidebar.visible'
const WIDTH_KEY = 'pirateboys.sidebar.width'

function readVisible(): boolean {
  try {
    return window.localStorage.getItem(VISIBLE_KEY) !== 'false'
  } catch {
    return true
  }
}

function readWidth(): number {
  try {
    const stored = Number.parseInt(window.localStorage.getItem(WIDTH_KEY) ?? '', 10)
    if (!Number.isFinite(stored)) return SIDEBAR_WIDTH
    return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, stored))
  } catch {
    return SIDEBAR_WIDTH
  }
}

function persist(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // private mode — the layout simply won't survive a relaunch
  }
}

/** True when the event is a ⌘<key> chord with exactly the modifiers asked for. */
function isChord(event: KeyboardEvent, key: string, shift = false): boolean {
  return (
    event.metaKey && !event.ctrlKey && !event.altKey && event.shiftKey === shift && event.key.toLowerCase() === key
  )
}

/**
 * The app shell. One column:
 *
 *   ┌ Titlebar (34px, drag region, traffic-light-aware clusters) ┐
 *   ├ Sidebar (resizable) │ content (flex-1, own scroll) ────────┤
 *   └ Statusbar (22px, engine state + version) ──────────────────┘
 *
 * The shell root carries `.pb-app` — the scope that turns on the design
 * system's font, background, selection-stripping and scrollbars — and publishes
 * the live sidebar width as `--pb-sidebar-width`, so CSS and TS never disagree
 * about it.
 *
 * The tooltip and toast providers deliberately live ABOVE this component (in
 * `App`), not inside it. They used to mount here, which put everything that
 * COMPOSES the shell — the sidebar section hooks in particular — outside the
 * toast provider, so a sidebar row action could do its work and then have
 * nowhere to say so (defect D1). A provider that only covers the shell's
 * children cannot serve the shell's inputs.
 *
 * Sidebar visibility and width persist (`pirateboys.sidebar.visible` /
 * `.width`), and collapsing animates the WRAPPER's width with the aside kept
 * mounted: a conditional render snaps and reflows every row on the way out.
 *
 * Owns the shell chords, so a key and its button always invoke the same action:
 *   ⌘B sidebar · ⌘K palette · ⌘, settings · ⌘N new chat · ⇧⌘N new scan
 * ⌘B, ⌘K and ⌘, TOGGLE — the key that summons a surface is the key that
 * dismisses it, so nobody has to reach for Esc to undo a mis-press.
 * In the packaged app these are native menu accelerators and the menu owns
 * them outright — the keydown listener stands down there (`isDesktop()`), so a
 * toggle can never be flipped twice by one press. The listener is what makes
 * the same chords work in the browser sandbox and the dev server.
 */
export function AppShell({
  banner,
  children,
  chordsEnabled = true,
  onNewChat,
  onNewScan,
  sidebar,
  statusbar,
  titlebar,
}: AppShellProps) {
  const [sidebarVisible, setSidebarVisible] = useState(readVisible)
  const [sidebarWidth, setSidebarWidth] = useState(readWidth)
  const [resizing, setResizing] = useState(false)
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null)
  // The width at the instant the drag ends — state may not have flushed yet.
  const widthRef = useRef(sidebarWidth)
  const sidebarPaneRef = useRef<HTMLDivElement>(null)

  const toggleSidebar = useCallback(() => setSidebarVisible((visible) => !visible), [])

  // Persisting inside the state updater would write during render (updaters
  // must be pure, and StrictMode runs them twice). The effect is the one place
  // that owns the side effect.
  useEffect(() => {
    persist(VISIBLE_KEY, String(sidebarVisible))
  }, [sidebarVisible])

  // A collapsed sidebar is still MOUNTED (that is what makes the width animate),
  // so it has to be taken out of the tab order and the a11y tree by hand —
  // otherwise Tab walks into rows nobody can see. `inert` is set imperatively
  // because React 18's JSX types don't carry the attribute yet.
  //
  // Collapsing can also happen while focus is INSIDE the rail (⌘B from a chat
  // row). `inert` would then blur it to nowhere and the next Tab would restart
  // from the top of the document, so focus is handed to the control that just
  // hid it — which is also the control that brings it back.
  useEffect(() => {
    const pane = sidebarPaneRef.current
    if (pane === null) return
    if (sidebarVisible) {
      pane.removeAttribute('inert')
      return
    }
    const focusWasInside = pane.contains(document.activeElement)
    pane.setAttribute('inert', '')
    if (focusWasInside) {
      document.querySelector<HTMLElement>('[data-slot="sidebar-toggle"]')?.focus()
    }
  }, [sidebarVisible])

  const { onTogglePalette, onToggleSettings } = titlebar ?? {}

  // ⌘B / ⌘K / ⌘, / ⌘N / ⇧⌘N are ALSO native menu accelerators, and all of them
  // toggle. Inside the shell the menu owns the keys — and if both paths ever
  // fired, the toggle would flip twice and read as a dead key. So this listener
  // exists for the BROWSER sandbox (vite dev, the screenshot harness), which
  // has no menu at all, and stands down in the packaged app. The menu route is
  // the `onMenuAction` subscription below.
  useEffect(() => {
    if (!chordsEnabled || isDesktop()) return
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target
      const editable =
        target instanceof HTMLElement && target.closest('input, textarea, [contenteditable="true"]') !== null

      if (isChord(event, 'b') && !editable) {
        event.preventDefault()
        toggleSidebar()
      } else if (isChord(event, 'k') && onTogglePalette !== undefined) {
        event.preventDefault()
        onTogglePalette()
      } else if (isChord(event, 'n') && onNewChat !== undefined) {
        event.preventDefault()
        onNewChat()
      } else if (isChord(event, 'n', true) && onNewScan !== undefined) {
        event.preventDefault()
        onNewScan()
      } else if (event.metaKey && event.key === ',' && !event.ctrlKey && !event.altKey && onToggleSettings !== undefined) {
        event.preventDefault()
        onToggleSettings()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [chordsEnabled, onNewChat, onNewScan, onTogglePalette, onToggleSettings, toggleSidebar])

  // The desktop route for the same five actions. Inside the shell this is the
  // ONLY route — the keydown listener above stands down there — so a menu item
  // and its accelerator can never both fire and flip a toggle twice.
  // 'about' and 'find' have no shell callback; App subscribes for those.
  useEffect(() => {
    if (!chordsEnabled) return
    return onMenuAction((action) => {
      switch (action) {
        case 'toggle-sidebar':
          toggleSidebar()
          break
        case 'command-palette':
          onTogglePalette?.()
          break
        case 'open-settings':
          onToggleSettings?.()
          break
        case 'new-chat':
          onNewChat?.()
          break
        case 'new-scan':
          onNewScan?.()
          break
        default:
          break
      }
    })
  }, [chordsEnabled, onNewChat, onNewScan, onTogglePalette, onToggleSettings, toggleSidebar])

  const onSashPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    dragRef.current = { startX: event.clientX, startWidth: sidebarWidth }
    setResizing(true)
  }

  const onSashPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current
    if (drag === null) return
    const next = Math.min(
      SIDEBAR_MAX_WIDTH,
      Math.max(SIDEBAR_MIN_WIDTH, drag.startWidth + (event.clientX - drag.startX)),
    )
    widthRef.current = next
    setSidebarWidth(next)
  }

  const endSashDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (dragRef.current === null) return
    dragRef.current = null
    setResizing(false)
    event.currentTarget.releasePointerCapture(event.pointerId)
    persist(WIDTH_KEY, String(widthRef.current))
  }

  // ⌘B during a drag unmounts the sash, and neither pointerup nor pointercancel
  // fires on unmount — so `endSashDrag` never runs. The stale drag state then
  // outlives the gesture: the width transition stays suppressed for the rest of
  // the session, and merely HOVERING the sash later re-enters the move handler
  // with a live `dragRef` and snaps the width with no button pressed.
  useEffect(() => {
    if (sidebarVisible) return
    dragRef.current = null
    setResizing(false)
  }, [sidebarVisible])

  const shellActions = React.useMemo<ShellActions>(
    () => ({ toggleSidebar, sidebarVisible }),
    [sidebarVisible, toggleSidebar],
  )

  return (
    <ShellActionsContext.Provider value={shellActions}>
      <div
        className="pb-app flex h-screen w-screen flex-col overflow-hidden"
        style={{ '--pb-sidebar-width': `${sidebarWidth}px` } as React.CSSProperties}
      >
        <Titlebar
          onToggleSidebar={toggleSidebar}
          sidebarVisible={sidebarVisible}
          {...titlebar}
          {...(onNewChat !== undefined ? { onNewChat } : {})}
        />
        <div className="flex min-h-0 w-full flex-1">
          <div
            className={cn('relative shrink-0 overflow-hidden', !resizing && 'pb-sidebar-transition')}
            data-slot="sidebar-dock"
            data-visible={sidebarVisible}
            style={{ width: sidebarVisible ? sidebarWidth : 0 }}
          >
            <div
              aria-hidden={!sidebarVisible}
              className="h-full"
              ref={sidebarPaneRef}
              style={{ width: sidebarWidth }}
            >
              <Sidebar {...sidebar} />
            </div>
            {sidebarVisible ? (
              <div
                aria-hidden
                className="absolute inset-y-0 right-0 z-10 w-1 cursor-col-resize hover:bg-accent-soft"
                data-slot="sidebar-sash"
                onPointerCancel={endSashDrag}
                onPointerDown={onSashPointerDown}
                onPointerMove={onSashPointerMove}
                onPointerUp={endSashDrag}
              />
            ) : null}
          </div>
          {/* The banner is a SIBLING of the scroll container, not its first
              child: an offline notice that scrolls away is one the user
              stops seeing exactly when they start reading. */}
          <div className="flex min-w-0 flex-1 flex-col bg-surface-chrome">
            {banner}
            <main className="pb-scroll min-w-0 flex-1 overflow-y-auto" data-slot="shell-content">
              {children}
            </main>
          </div>
        </div>
    {statusbar !== undefined ? <Statusbar {...statusbar} /> : null}
      </div>
    </ShellActionsContext.Provider>
  )
}
