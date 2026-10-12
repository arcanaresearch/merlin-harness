/**
 * Shell geometry — the single source for chrome dimensions.
 *
 * The same numbers exist as CSS vars in src/styles.css (--pb-titlebar-height
 * etc.) for use inside stylesheets; these constants are for TS layout math.
 * Keep the two in sync in the same change.
 */

export const TITLEBAR_HEIGHT = 34

/**
 * Fallback macOS traffic-light origin. The real one is measured at runtime via
 * `getWindowButtonPosition()` — macOS Tahoe moved the buttons, so a hardcoded
 * inset put our first tool underneath them.
 */
export const TRAFFIC_LIGHTS_X = 24

/** Distance from the traffic-light origin to the first titlebar tool. */
export const TITLEBAR_CONTROL_OFFSET_X = 74

/**
 * Left-cluster inset used when there is no window to measure — the plain
 * browser sandbox (vite dev, the screenshot harness), which has no traffic
 * lights to dodge in the first place.
 */
export const TITLEBAR_LEFT_CLUSTER_X = TITLEBAR_CONTROL_OFFSET_X // 74

/**
 * Inset used when the shell reports NO window buttons — macOS fullscreen (the
 * lights are hidden) or off-mac. There is nothing to dodge, so the cluster
 * pins to the window edge with the same 14px Hermes uses for that case.
 */
export const TITLEBAR_EDGE_INSET = 14

/** Titlebar tool hit target (both axes). */
export const TITLEBAR_CONTROL_SIZE = 24

export const SIDEBAR_WIDTH = 237
/** Drag-resize clamp for the sidebar. */
export const SIDEBAR_MIN_WIDTH = 200
export const SIDEBAR_MAX_WIDTH = 320

export const STATUSBAR_HEIGHT = 22

/**
 * The horizontal inset of the content column. It grows with the window instead
 * of stepping at breakpoints. Anything that must line up with page content but
 * sits OUTSIDE the frame — the engine offline banner, a sticky toolbar — uses
 * this rather than retyping the clamp.
 */
export const PAGE_INSET_X = 'px-[clamp(1.25rem,4vw,3rem)]'

/**
 * The ONE page frame. Every full-page screen (History, Scan detail, Reports,
 * Targets, Library) wraps its content in this so widths, insets and rhythm
 * cannot drift screen to screen:
 *
 *   <div className={PAGE_FRAME_CLASS}> <PageHeader …/> … </div>
 *
 * Column is capped at max-w-5xl and centred. The string is written out in full
 * rather than composed from PAGE_INSET_X: every screen must resolve to the
 * BYTE-IDENTICAL class list, and the audit found two column widths and two left
 * edges across five sibling pages when they each built their own.
 */
export const PAGE_FRAME_CLASS =
  'mx-auto flex w-full max-w-5xl flex-col gap-8 px-[clamp(1.25rem,4vw,3rem)] py-8'
