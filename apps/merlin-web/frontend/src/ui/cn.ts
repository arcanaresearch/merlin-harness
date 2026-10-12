import { clsx, type ClassValue } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

/**
 * The custom type scale (`--text-pb-*` in styles.css). These MUST be declared
 * to tailwind-merge, because it cannot know they are font sizes.
 *
 * Left undeclared, `text-pb-caption` falls into tailwind-merge's `text-color`
 * group — it matches the arbitrary-value validator that also catches
 * `text-accent-foreground` — so the two are treated as conflicting and the last
 * one wins. That silently broke Button in both directions: sizes carrying a
 * scale class (xs / lg / micro) evicted `text-accent-foreground`, leaving the
 * accent fill with inherited ink (measured 1.15:1 on the offline banner), and
 * `size="default"` evicted `text-pb-caption` instead, dropping the font size.
 *
 * Keep this list in sync with the `--text-pb-*` tokens.
 */
const PB_TYPE_SCALE = ['display', 'page', 'section', 'body', 'caption', 'micro'] as const

const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      'font-size': [{ 'text-pb': [...PB_TYPE_SCALE] }],
    },
  },
})

/**
 * Class combiner for the design system. The only sanctioned way to merge
 * conditional classes onto a primitive.
 *
 * **Adding a custom `text-*` utility? Register it above.** Anything the merge
 * does not recognise is guessed at, and a wrong guess silently deletes a class
 * at runtime — with no type error and no build failure to catch it.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
