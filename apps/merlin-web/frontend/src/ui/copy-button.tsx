import * as React from 'react'

import { Button } from './button'
import { cn } from './cn'
import { CheckGlyph, CopyGlyph } from './glyphs'

/** How long the copied checkmark stays before reverting to the copy mark. */
const COPIED_MS = 1_200

/**
 * Writes text to the clipboard. Throws when no clipboard is available so the
 * caller can surface a real failure instead of silently doing nothing.
 */
export async function writeClipboardText(value: string): Promise<void> {
  const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard
  if (clipboard === undefined) throw new Error('Clipboard is not available.')
  await clipboard.writeText(value)
}

export interface CopyButtonProps {
  /** The exact text placed on the clipboard. */
  value: string
  /** Accessible name and Tip label. Defaults to "Copy". */
  label?: string
  size?: 'icon-xs' | 'icon-sm'
  /**
   * Pass `-1` to take the button out of the tab order — for a per-row copy
   * inside a list that is itself arrow-navigated, where a tab stop on every row
   * would bury the row's own controls. Leave unset everywhere else: a copy
   * action a keyboard user cannot reach is a copy action they do not have.
   */
  tabIndex?: number
  className?: string
}

/**
 * The ONE copy affordance. An icon button that swaps its own glyph to a check
 * for 1.2s — the feedback lives IN the control, never only in a tooltip and
 * never as a toast, so one verb has one behaviour everywhere.
 *
 * For a labelled copy action ("Copy scan id") use a `Button` with
 * `<CopyGlyph/>` and the same swap, or place this next to the value.
 */
export function CopyButton({ className, label = 'Copy', size = 'icon-xs', tabIndex, value }: CopyButtonProps) {
  const [copied, setCopied] = React.useState(false)
  const [failed, setFailed] = React.useState(false)
  const timer = React.useRef<number | undefined>(undefined)
  const mounted = React.useRef(true)

  React.useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      window.clearTimeout(timer.current)
      timer.current = undefined
    }
  }, [])

  async function copy() {
    window.clearTimeout(timer.current)
    try {
      await writeClipboardText(value)
      // The clipboard write is async: the row can unmount mid-flight (a list
      // re-sorts, a dialog closes). Without this the handler would set state on
      // a dead component AND arm a timer the cleanup has already run past.
      if (!mounted.current) return
      setFailed(false)
      setCopied(true)
    } catch {
      if (!mounted.current) return
      setCopied(false)
      setFailed(true)
    }
    timer.current = window.setTimeout(() => {
      setCopied(false)
      setFailed(false)
    }, COPIED_MS)
  }

  return (
    <Button
      aria-label={copied ? 'Copied' : label}
      className={cn(failed && 'text-sev-red', className)}
      data-copied={copied || undefined}
      onClick={() => void copy()}
      size={size}
      tabIndex={tabIndex}
      type="button"
      variant="ghost"
    >
      {copied ? <CheckGlyph className="text-sev-green" /> : <CopyGlyph />}
    </Button>
  )
}
