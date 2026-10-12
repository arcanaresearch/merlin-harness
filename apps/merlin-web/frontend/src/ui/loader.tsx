import { cn } from './cn'

export interface LoaderProps {
  /** Diameter in px. Default 24. */
  size?: number
  /** Accessible label (announced via role="status"). Visually there is NEVER
   *  a "Loading…" text — the mark alone carries the state. */
  label?: string
  /** Drops role/label and hides the mark from a11y — for loaders nested inside
   *  a control that already announces its own state (e.g. `<Button busy>`,
   *  which sets aria-busy). Prevents "Working" leaking into the button's name. */
  decorative?: boolean
  className?: string
}

/**
 * The signature loader: an animated sonar sweep — a rotating conic beam plus
 * two expanding range rings, drawn in text-primary so it tracks every theme
 * and surface. Fitting for a scanner; no literal "Loading…" ever.
 *
 * The sweep + rings are pure CSS (pb-sonar-sweep / pb-sonar-ring in
 * styles.css) and freeze under prefers-reduced-motion like everything else.
 */
export function Loader({ className, decorative = false, label = 'Working', size = 24 }: LoaderProps) {
  return (
    <span
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : label}
      className={cn('inline-flex items-center justify-center text-content-primary', className)}
      data-slot="loader"
      role={decorative ? undefined : 'status'}
      style={{ height: size, width: size }}
    >
      <svg aria-hidden className="size-full overflow-visible" fill="none" viewBox="0 0 24 24">
        {/* range rings */}
        <circle cx="12" cy="12" r="10.5" stroke="currentColor" strokeOpacity="0.14" strokeWidth="1" />
        <circle cx="12" cy="12" r="6.5" stroke="currentColor" strokeOpacity="0.14" strokeWidth="1" />
        {/* expanding pulse ring */}
        <circle
          cx="12"
          cy="12"
          r="10.5"
          stroke="currentColor"
          strokeWidth="1"
          style={{
            animation: 'pb-sonar-ring 2s var(--pb-ease-out) infinite',
            transformOrigin: 'center',
          }}
        />
        {/* rotating sweep beam */}
        <g style={{ animation: 'pb-sonar-sweep 1.8s linear infinite', transformOrigin: 'center' }}>
          <path d="M12 12 12 1.5" stroke="currentColor" strokeLinecap="round" strokeWidth="1.6" />
          <path d="M12 12 12 1.5" stroke="currentColor" strokeLinecap="round" strokeOpacity="0.35" strokeWidth="4" />
        </g>
        {/* center contact */}
        <circle cx="12" cy="12" r="1.4" fill="currentColor" />
      </svg>
    </span>
  )
}
