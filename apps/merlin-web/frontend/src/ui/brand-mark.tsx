import { cn } from './cn'

export interface BrandMarkProps {
  /** Tile edge in px. Default 40. The mark scales with it. */
  size?: number
  className?: string
}

/**
 * The product mark: a shield holding a radar sweep, on a raised rounded tile.
 * Monochrome `currentColor` for the shield with one accent ring for the sweep,
 * so it reads correctly on every surface and in both themes.
 *
 * Use it where the product introduces itself — the onboarding welcome, About,
 * and the Home intro hero. It is NOT a status icon and never sits in a list
 * row or a button.
 */
export function BrandMark({ className, size = 40 }: BrandMarkProps) {
  return (
    <span
      aria-hidden
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-[10px] bg-surface-elevated text-content-primary shadow-pb-sm',
        className,
      )}
      data-slot="brand-mark"
      style={{ height: size, width: size }}
    >
      <svg
        className="overflow-visible"
        fill="none"
        height={Math.round(size * 0.55)}
        viewBox="0 0 24 24"
        width={Math.round(size * 0.55)}
      >
        {/* shield */}
        <path
          d="M19.5 12.4c0 4.7-3.3 7.05-7.26 8.43a.9.9 0 0 1-.63 0C7.8 19.45 4.5 17.1 4.5 12.4V6.15a.9.9 0 0 1 .9-.9c1.9 0 4.27-1.13 5.92-2.56a1.06 1.06 0 0 1 1.36 0c1.65 1.43 4.02 2.56 5.92 2.56a.9.9 0 0 1 .9.9Z"
          stroke="currentColor"
          strokeLinejoin="round"
          strokeWidth={1.6}
        />
        {/* radar rings + sweep */}
        <circle cx="12" cy="11.6" r="4.4" stroke="var(--ui-accent)" strokeOpacity="0.45" strokeWidth={1.2} />
        <path d="m12 11.6 3.1-3.1" stroke="var(--ui-accent)" strokeLinecap="round" strokeWidth={1.6} />
        <circle cx="12" cy="11.6" fill="var(--ui-accent)" r="1.2" />
      </svg>
    </span>
  )
}
