import * as React from 'react'

/**
 * The ONE inline glyph set for the design system, shell chrome and feature
 * screens. Every mark is drawn on a 24-unit grid inside a 3–21 safe area,
 * 1.75px stroke, round caps and joins, `currentColor` — so glyphs inherit size
 * and colour from the primitive that hosts them (Button caps them at size-3.5,
 * size-3 at xs).
 *
 * Rules:
 *   - Need a glyph that isn't here? Add it HERE, in this style. Never fork a
 *     per-feature glyph file and never pull an icon package into feature code.
 *   - One mark per verb. `RefreshGlyph` is an alias of `RetryGlyph` (retry and
 *     refresh are the same circular-arrow verb) — do not draw a second one.
 *   - Every glyph forwards its ref, so a glyph can be the direct child of a
 *     Radix `asChild` trigger (`<Tip>`, `<DropdownMenuTrigger>`) without the
 *     Popper anchor being dropped.
 *   - Dots and filled details use `fill` at r ≥ 1.5 rather than a stroked
 *     hairline circle, which optically bloats at 14px.
 */

export type GlyphProps = React.ComponentProps<'svg'>

/**
 * Builds a ref-forwarding glyph from its path content. Keeps every mark on one
 * set of stroke defaults; callers may still override any SVG prop.
 */
function glyph(displayName: string, paths: React.ReactNode) {
  const Component = React.forwardRef<SVGSVGElement, GlyphProps>((props, ref) => (
    <svg
      aria-hidden
      fill="none"
      ref={ref}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={1.75}
      viewBox="0 0 24 24"
      {...props}
    >
      {paths}
    </svg>
  ))
  Component.displayName = displayName
  return Component
}

/* ── Chrome ─────────────────────────────────────────────────────────────── */

export const SearchGlyph = glyph(
  'SearchGlyph',
  <>
    <circle cx="10.5" cy="10.5" r="6.5" />
    <path d="m20 20-4.7-4.7" />
  </>,
)

export const XGlyph = glyph(
  'XGlyph',
  <>
    <path d="M18 6 6 18" />
    <path d="m6 6 12 12" />
  </>,
)

export const PlusGlyph = glyph(
  'PlusGlyph',
  <>
    <path d="M12 5v14" />
    <path d="M5 12h14" />
  </>,
)

export const PanelLeftGlyph = glyph(
  'PanelLeftGlyph',
  <>
    <rect height="16" rx="2" width="18" x="3" y="4" />
    <path d="M9 4v16" />
  </>,
)

/** ⌘ modifier mark. Chrome only — never as the "command palette" affordance
 *  (a consumer doesn't read ⌘ as "search everything"; use SearchGlyph). */
export const CommandGlyph = glyph(
  'CommandGlyph',
  <path d="M15 6.5v11a2.5 2.5 0 1 0 2.5-2.5h-11a2.5 2.5 0 1 0 2.5 2.5v-11a2.5 2.5 0 1 0-2.5 2.5h11a2.5 2.5 0 1 0-2.5-2.5" />,
)

export const SunGlyph = glyph(
  'SunGlyph',
  <>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 3.5v1.6" />
    <path d="M12 18.9v1.6" />
    <path d="m5.99 5.99 1.13 1.13" />
    <path d="m16.88 16.88 1.13 1.13" />
    <path d="M3.5 12h1.6" />
    <path d="M18.9 12h1.6" />
    <path d="m7.12 16.88-1.13 1.13" />
    <path d="m18.01 5.99-1.13 1.13" />
  </>,
)

export const MoonGlyph = glyph('MoonGlyph', <path d="M12.5 3.5a6.5 6.5 0 0 0 8 8 8.5 8.5 0 1 1-8-8Z" />)

export const SettingsGlyph = glyph(
  'SettingsGlyph',
  <>
    <path d="M12.2 3h-.4a1.8 1.8 0 0 0-1.8 1.8v.1a1.8 1.8 0 0 1-.9 1.56l-.36.2a1.8 1.8 0 0 1-1.8 0l-.1-.05a1.8 1.8 0 0 0-2.45.66l-.2.35a1.8 1.8 0 0 0 .66 2.45l.1.06a1.8 1.8 0 0 1 .9 1.55v.42a1.8 1.8 0 0 1-.9 1.56l-.1.06a1.8 1.8 0 0 0-.66 2.45l.2.35a1.8 1.8 0 0 0 2.45.66l.1-.06a1.8 1.8 0 0 1 1.8 0l.36.21a1.8 1.8 0 0 1 .9 1.55v.1A1.8 1.8 0 0 0 11.8 21h.4a1.8 1.8 0 0 0 1.8-1.8v-.1a1.8 1.8 0 0 1 .9-1.55l.36-.21a1.8 1.8 0 0 1 1.8 0l.1.06a1.8 1.8 0 0 0 2.45-.66l.2-.35a1.8 1.8 0 0 0-.66-2.45l-.1-.06a1.8 1.8 0 0 1-.9-1.56v-.42a1.8 1.8 0 0 1 .9-1.55l.1-.06a1.8 1.8 0 0 0 .66-2.45l-.2-.35a1.8 1.8 0 0 0-2.45-.66l-.1.05a1.8 1.8 0 0 1-1.8 0l-.36-.2a1.8 1.8 0 0 1-.9-1.56v-.1A1.8 1.8 0 0 0 12.2 3Z" />
    <circle cx="12" cy="12" r="2.75" />
  </>,
)

export const MoreHorizontalGlyph = glyph(
  'MoreHorizontalGlyph',
  <>
    <circle cx="5.5" cy="12" fill="currentColor" r="1.5" stroke="none" />
    <circle cx="12" cy="12" fill="currentColor" r="1.5" stroke="none" />
    <circle cx="18.5" cy="12" fill="currentColor" r="1.5" stroke="none" />
  </>,
)

export const ChevronRightGlyph = glyph('ChevronRightGlyph', <path d="m9.5 5.5 6.5 6.5-6.5 6.5" />)

export const ChevronLeftGlyph = glyph('ChevronLeftGlyph', <path d="M14.5 5.5 8 12l6.5 6.5" />)

export const ChevronDownGlyph = glyph('ChevronDownGlyph', <path d="M5.5 9.5 12 16l6.5-6.5" />)

export const ExternalLinkGlyph = glyph(
  'ExternalLinkGlyph',
  <>
    <path d="M14.5 3.5H21v6.5" />
    <path d="M10.5 13.5 21 3.5" />
    <path d="M18 13.5V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5.5" />
  </>,
)

/* ── Feedback ───────────────────────────────────────────────────────────── */

/** Canonical ERROR mark — the ONE error glyph. No background chip. */
export const ErrorGlyph = glyph(
  'ErrorGlyph',
  <>
    <path d="M13.5 4.1 20.9 17a1.75 1.75 0 0 1-1.5 2.6H4.6A1.75 1.75 0 0 1 3.1 17l7.4-12.9a1.75 1.75 0 0 1 3 0Z" />
    <path d="M12 9.5v4" />
    <path d="M12 16.5h.01" />
  </>,
)

/** Warning — same silhouette family as ErrorGlyph, drawn as a circle so a
 *  warning is never mistaken for an error at a glance. */
export const WarningGlyph = glyph(
  'WarningGlyph',
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.75v4.75" />
    <path d="M12 16.25h.01" />
  </>,
)

export const InfoGlyph = glyph(
  'InfoGlyph',
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 16.25v-4.75" />
    <path d="M12 7.75h.01" />
  </>,
)

export const CheckGlyph = glyph('CheckGlyph', <path d="M19.5 6.5 9.5 17l-5-5" />)

export const RadarGlyph = glyph(
  'RadarGlyph',
  <>
    <circle cx="12" cy="12" r="8.5" />
    <circle cx="12" cy="12" r="4.75" />
    <path d="m12 12 6-6" />
    <circle cx="12" cy="12" fill="currentColor" r="1.5" stroke="none" />
  </>,
)

export const InboxGlyph = glyph(
  'InboxGlyph',
  <>
    <path d="M21 12h-5l-1.75 2.75h-4.5L8 12H3" />
    <path d="M6.1 5.1 3 12v5.5a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V12l-3.1-6.9A2 2 0 0 0 16.1 4H7.9a2 2 0 0 0-1.8 1.1Z" />
  </>,
)

export const BellGlyph = glyph(
  'BellGlyph',
  <>
    <path d="M18 9.5a6 6 0 1 0-12 0c0 5-2 6.5-2 6.5h16s-2-1.5-2-6.5Z" />
    <path d="M13.75 20a2 2 0 0 1-3.5 0" />
  </>,
)

/* ── Domain ─────────────────────────────────────────────────────────────── */

export const TargetGlyph = glyph(
  'TargetGlyph',
  <>
    <circle cx="12" cy="12" r="8.5" />
    <circle cx="12" cy="12" r="4.75" />
    <circle cx="12" cy="12" fill="currentColor" r="1.6" stroke="none" />
  </>,
)

export const FolderGlyph = glyph(
  'FolderGlyph',
  <path d="M19 19.5a2 2 0 0 0 2-2v-9a2 2 0 0 0-2-2h-7.1a2 2 0 0 1-1.66-.89l-.68-1.02a2 2 0 0 0-1.67-.89H5a2 2 0 0 0-2 2v11.8a2 2 0 0 0 2 2Z" />,
)

export const ListGlyph = glyph(
  'ListGlyph',
  <>
    <path d="M8.5 6.5H21" />
    <path d="M8.5 12H21" />
    <path d="M8.5 17.5H21" />
    <path d="M3.5 6.5h.01" />
    <path d="M3.5 12h.01" />
    <path d="M3.5 17.5h.01" />
  </>,
)

/** Open book. Kept inside the 3–21 safe area so it doesn't read wider than its
 *  sidebar siblings (it used to span 2–22). */
export const BookGlyph = glyph(
  'BookGlyph',
  <>
    <path d="M3 4.5h5a3.5 3.5 0 0 1 3.5 3.5v11.5a2.75 2.75 0 0 0-2.75-2.5H3Z" />
    <path d="M21 4.5h-5A3.5 3.5 0 0 0 12.5 8v11.5a2.75 2.75 0 0 1 2.75-2.5H21Z" />
  </>,
)

export const FileTextGlyph = glyph(
  'FileTextGlyph',
  <>
    <path d="M13.5 3H6.5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V9Z" />
    <path d="M13.5 3v6h6" />
    <path d="M15.5 13.5h-7" />
    <path d="M15.5 17h-7" />
    <path d="M10.5 10h-2" />
  </>,
)

export const ChipGlyph = glyph(
  'ChipGlyph',
  <>
    <rect height="14" rx="2" width="14" x="5" y="5" />
    <rect height="5" width="5" x="9.5" y="9.5" />
    <path d="M15 3v2" />
    <path d="M15 19v2" />
    <path d="M3 15h2" />
    <path d="M3 9h2" />
    <path d="M19 15h2" />
    <path d="M19 9h2" />
    <path d="M9 3v2" />
    <path d="M9 19v2" />
  </>,
)

export const FlaskGlyph = glyph(
  'FlaskGlyph',
  <>
    <path d="M10 3.5v6.03a2 2 0 0 1-.21.89l-4.42 8.83a1 1 0 0 0 .9 1.45h11.46a1 1 0 0 0 .9-1.45l-4.42-8.83a2 2 0 0 1-.21-.89V3.5" />
    <path d="M8.75 3.5h6.5" />
    <path d="M7.25 15.5h9.5" />
  </>,
)

export const ArchiveGlyph = glyph(
  'ArchiveGlyph',
  <>
    <rect height="4.5" rx="1" width="18" x="3" y="3.5" />
    <path d="M4.75 8v11a2 2 0 0 0 2 2h10.5a2 2 0 0 0 2-2V8" />
    <path d="M10.25 12h3.5" />
  </>,
)

/** Chat bubble — the home surface / conversation mark. */
export const ChatGlyph = glyph(
  'ChatGlyph',
  <path d="M20.5 11.6a7.9 7.9 0 0 1-8.4 7.9 8 8 0 0 1-3.4-.85L3.5 20.5l1.85-5.2a7.9 7.9 0 0 1-.85-3.6 7.9 7.9 0 0 1 8-7.9h.5a7.9 7.9 0 0 1 7.5 7.5Z" />,
)

/** Send (paper plane) — the composer submit affordance. */
export const SendGlyph = glyph(
  'SendGlyph',
  <>
    <path d="m21 3-6.5 18-3.7-8.3L3 9Z" />
    <path d="M21 3 10.8 12.7" />
  </>,
)

/** Play — launch a scan. */
export const PlayGlyph = glyph('PlayGlyph', <path d="M6.5 4.2 19 12 6.5 19.8Z" />)

/** Square — stop / cancel a running scan. */
export const StopGlyph = glyph('StopGlyph', <rect height="14" rx="2" width="14" x="5" y="5" />)

/** Key — your AI key / credential affordances. */
export const KeyGlyph = glyph(
  'KeyGlyph',
  <>
    <circle cx="8.25" cy="15.75" r="4.25" />
    <path d="m11.25 12.75 8-8" />
    <path d="m16.5 7.5 2.25 2.25" />
    <path d="M19.25 4.75 21 6.5" />
  </>,
)

/** Bolt — assistant activity, inline suggestions. */
export const BoltGlyph = glyph('BoltGlyph', <path d="M13 3 4.5 13.5H12l-1 7.5L19.5 10.5H12Z" />)

/** Sparkle — the brand-adjacent "new / intelligent" mark (hero, onboarding). */
export const SparkleGlyph = glyph(
  'SparkleGlyph',
  <>
    <path d="M12 3.5 13.9 9 19.5 11l-5.6 2L12 18.5 10.1 13 4.5 11l5.6-2Z" />
    <path d="M18.5 16.5 19.2 18.3 21 19l-1.8.7-.7 1.8-.7-1.8L16 19l1.8-.7Z" />
  </>,
)

export const ClockGlyph = glyph(
  'ClockGlyph',
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.25V12l3.25 1.9" />
  </>,
)

/** Shield — the safety / scan-awareness affordance. */
export const ShieldGlyph = glyph(
  'ShieldGlyph',
  <path d="M19.5 12.5c0 4.75-3.3 7.1-7.26 8.48a.9.9 0 0 1-.63 0C7.8 19.6 4.5 17.25 4.5 12.5V6.2a.9.9 0 0 1 .9-.9c1.9 0 4.27-1.14 5.92-2.58a1.06 1.06 0 0 1 1.36 0c1.65 1.44 4.02 2.58 5.92 2.58a.9.9 0 0 1 .9.9Z" />,
)

/** Shield with a check — "verified safe", the approved/allowed state. */
export const ShieldCheckGlyph = glyph(
  'ShieldCheckGlyph',
  <>
    <path d="M19.5 12.5c0 4.75-3.3 7.1-7.26 8.48a.9.9 0 0 1-.63 0C7.8 19.6 4.5 17.25 4.5 12.5V6.2a.9.9 0 0 1 .9-.9c1.9 0 4.27-1.14 5.92-2.58a1.06 1.06 0 0 1 1.36 0c1.65 1.44 4.02 2.58 5.92 2.58a.9.9 0 0 1 .9.9Z" />
    <path d="m9.25 11.75 2 2 3.5-3.5" />
  </>,
)

/** Lock — private / on-this-Mac / nothing leaves. */
export const LockGlyph = glyph(
  'LockGlyph',
  <>
    <rect height="10" rx="2" width="14" x="5" y="10.5" />
    <path d="M8.25 10.5V7.75a3.75 3.75 0 0 1 7.5 0v2.75" />
  </>,
)

/** Monitor — this Mac / a local application target. */
export const MonitorGlyph = glyph(
  'MonitorGlyph',
  <>
    <rect height="12" rx="2" width="18" x="3" y="4" />
    <path d="M8.5 20h7" />
    <path d="M12 16v4" />
  </>,
)

/** Globe — an internet target / something that leaves this Mac. */
export const GlobeGlyph = glyph(
  'GlobeGlyph',
  <>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M3.5 12h17" />
    <path d="M12 3.5a13 13 0 0 1 0 17 13 13 0 0 1 0-17Z" />
  </>,
)

/* ── Verbs ──────────────────────────────────────────────────────────────── */

/** Copy to clipboard. Pair with CheckGlyph for the copied beat (see CopyButton). */
export const CopyGlyph = glyph(
  'CopyGlyph',
  <>
    <rect height="13" rx="2" width="13" x="8" y="8" />
    <path d="M4.5 16A2 2 0 0 1 3 14V5a2 2 0 0 1 2-2h9a2 2 0 0 1 2 1.5" />
  </>,
)

/** Retry / refresh — the single circular-arrow verb. `RefreshGlyph` is an
 *  alias; there is no second refresh mark. */
export const RetryGlyph = glyph(
  'RetryGlyph',
  <>
    <path d="M3.5 12a8.5 8.5 0 1 0 2.5-6L3.5 8.5" />
    <path d="M3.5 3.5v5h5" />
  </>,
)

/** @see RetryGlyph — same mark, kept for the "refresh this list" reading. */
export const RefreshGlyph = RetryGlyph

export const PencilGlyph = glyph(
  'PencilGlyph',
  <>
    <path d="M16.5 3.5a2.65 2.65 0 1 1 3.75 3.75L7.5 20 3 21.25 4.25 16.75Z" />
    <path d="m14.75 5.25 3.75 3.75" />
  </>,
)

export const TrashGlyph = glyph(
  'TrashGlyph',
  <>
    <path d="M3.5 6.25h17" />
    <path d="M18.25 6.25V19a2 2 0 0 1-2 2H7.75a2 2 0 0 1-2-2V6.25" />
    <path d="M8.75 6.25V4.75a2 2 0 0 1 2-2h2.5a2 2 0 0 1 2 2v1.5" />
    <path d="M10.25 10.75v5.5" />
    <path d="M13.75 10.75v5.5" />
  </>,
)

/* ── Model capabilities ─────────────────────────────────────────────────── */

/** Brain — model capability: reasoning. */
export const BrainGlyph = glyph(
  'BrainGlyph',
  <>
    <path d="M12 5.5a2.75 2.75 0 1 0-5.5.1 3.7 3.7 0 0 0-2.3 5.3 3.7 3.7 0 0 0 .5 6.05A3.7 3.7 0 1 0 12 18Z" />
    <path d="M12 5.5a2.75 2.75 0 1 1 5.5.1 3.7 3.7 0 0 1 2.3 5.3 3.7 3.7 0 0 1-.5 6.05A3.7 3.7 0 1 1 12 18Z" />
    <path d="M14.75 13a4.1 4.1 0 0 1-2.75-3.65A4.1 4.1 0 0 1 9.25 13" />
  </>,
)

/** Wrench — model capability: tool calling. */
export const WrenchGlyph = glyph(
  'WrenchGlyph',
  <path d="M14.5 6.9a1 1 0 0 0 0 1.4l1.35 1.35a1 1 0 0 0 1.4 0l3.25-3.25a5.5 5.5 0 0 1-7.3 7.3l-6.3 6.3a2 2 0 0 1-2.8-2.8l6.3-6.3a5.5 5.5 0 0 1 7.3-7.3L14.5 6.9Z" />,
)

/** Image — model capability: image input. */
export const ImageGlyph = glyph(
  'ImageGlyph',
  <>
    <rect height="16" rx="2" width="16" x="4" y="4" />
    <circle cx="9.25" cy="9.25" r="1.75" />
    <path d="m20 15-2.9-2.9a2 2 0 0 0-2.83 0L6.5 20" />
  </>,
)
