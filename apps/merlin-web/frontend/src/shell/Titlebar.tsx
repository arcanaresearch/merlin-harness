import { useEffect, useState } from 'react'

import {
  getWindowButtonPosition,
  isDesktop,
  onWindowButtonPosition,
  type WindowButtonPosition,
} from '@/lib/desktop'
import { Button, CommandGlyph, PanelLeftGlyph, PlusGlyph, SettingsGlyph, Tip, TipHintLabel } from '@/ui'
import { SHELL_COPY } from './copy'
import {
  TITLEBAR_CONTROL_OFFSET_X,
  TITLEBAR_EDGE_INSET,
  TITLEBAR_LEFT_CLUSTER_X,
  TRAFFIC_LIGHTS_X,
} from './layout-constants'

export interface TitlebarProps {
  /** Toggle the sidebar. */
  onToggleSidebar?: () => void
  /** Start a new chat. Rendered in the titlebar only while the sidebar is
   *  hidden — collapsing the rail must not take the action away with it. */
  onNewChat?: () => void
  /** True while the sidebar is visible (drives the New chat fallback). */
  sidebarVisible?: boolean
  /** Toggle the command palette — the same key/button opens and closes it. */
  onTogglePalette?: () => void
  /** Toggle the settings overlay. */
  onToggleSettings?: () => void
}

/**
 * Where the left cluster starts.
 *
 * macOS Tahoe moved the traffic lights, so a hardcoded inset put our first tool
 * underneath them. The shell measures the real origin and pushes it here:
 *
 *   - a position → sit 74px right of it (the lights are 3 buttons + gaps wide);
 *   - `null` in the shell → macOS fullscreen (lights hidden) or off-mac: pin to
 *     the window edge with the same inset the right cluster uses;
 *   - no shell at all (browser sandbox) → the static fallback; there are no
 *     traffic lights to dodge there.
 */
function useLeftClusterX(): number {
  const [position, setPosition] = useState<WindowButtonPosition | null | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    getWindowButtonPosition()
      .then((next) => {
        if (!cancelled) setPosition(next)
      })
      .catch(() => {
        // Geometry is decoration: if the shell cannot report it, the fallback
        // inset is already correct for every non-mac case. Nothing to say.
        if (!cancelled) setPosition(null)
      })
    const unsubscribe = onWindowButtonPosition((next) => setPosition(next))
    return () => {
      cancelled = true
      unsubscribe()
    }
  }, [])

  if (!isDesktop()) return TITLEBAR_LEFT_CLUSTER_X
  if (position === undefined) return TRAFFIC_LIGHTS_X + TITLEBAR_CONTROL_OFFSET_X
  if (position === null) return TITLEBAR_EDGE_INSET
  return position.x + TITLEBAR_CONTROL_OFFSET_X
}

/**
 * The 34px integrated titlebar. The whole bar is a `-webkit-app-region: drag`
 * surface (the window is frameless); controls opt out with no-drag.
 *
 * The bar sits on `bg-surface-bar` — chrome mixed toward the sidebar — so the
 * titlebar, the page and the sidebar read as three planes without leaning on
 * heavier hairlines. Its tips open DOWNWARD; a tip above a 34px bar is off the
 * window.
 *
 * No centered title: it repeated the sidebar's selected row and the page's own
 * h1 on every screen, three names for one place. The bar carries only what the
 * page cannot: the sidebar toggle on the left, the palette and settings on the
 * right. The theme toggle moved to Settings › Appearance and the palette —
 * a preference changed twice a year should not hold prime chrome.
 *
 * The matching chords (⌘B / ⌘K / ⌘,) are owned by AppShell; these buttons call
 * the same callbacks — one action, one home — and all three toggle.
 */
export function Titlebar({
  onNewChat,
  onToggleSettings,
  onToggleSidebar,
  onTogglePalette,
  sidebarVisible = true,
}: TitlebarProps) {
  const leftClusterX = useLeftClusterX()

  return (
    <header
      className="relative flex h-(--pb-titlebar-height) w-full shrink-0 select-none items-center border-b border-stroke-tertiary bg-surface-bar [-webkit-app-region:drag]"
      data-slot="titlebar"
    >
      <div
        className="absolute inset-y-0 flex items-center gap-0.5 [-webkit-app-region:no-drag]"
        style={{ left: leftClusterX }}
      >
        {onToggleSidebar !== undefined ? (
          <Tip label={<TipHintLabel hint="⌘B" text={SHELL_COPY.toggleSidebar} />} side="bottom">
            <Button
              aria-label={SHELL_COPY.toggleSidebar}
              data-slot="sidebar-toggle"
              onClick={onToggleSidebar}
              size="icon-titlebar"
              variant="ghost"
            >
              <PanelLeftGlyph />
            </Button>
          </Tip>
        ) : null}
        {onNewChat !== undefined && !sidebarVisible ? (
          <Tip label={<TipHintLabel hint="⌘N" text={SHELL_COPY.newChat} />} side="bottom">
            <Button aria-label={SHELL_COPY.newChat} onClick={onNewChat} size="icon-titlebar" variant="ghost">
              <PlusGlyph />
            </Button>
          </Tip>
        ) : null}
      </div>

      <div className="absolute inset-y-0 right-3 flex items-center gap-0.5 [-webkit-app-region:no-drag]">
        {onTogglePalette !== undefined ? (
          <Tip label={<TipHintLabel hint="⌘K" text={SHELL_COPY.commandPalette} />} side="bottom">
            <Button aria-label={SHELL_COPY.commandPalette} onClick={onTogglePalette} size="icon-titlebar" variant="ghost">
              <CommandGlyph />
            </Button>
          </Tip>
        ) : null}
        {onToggleSettings !== undefined ? (
          <Tip label={<TipHintLabel hint="⌘," text={SHELL_COPY.settings} />} side="bottom">
            <Button aria-label={SHELL_COPY.settings} onClick={onToggleSettings} size="icon-titlebar" variant="ghost">
              <SettingsGlyph />
            </Button>
          </Tip>
        ) : null}
      </div>
    </header>
  )
}
