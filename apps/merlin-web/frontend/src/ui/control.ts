import { cva, type VariantProps } from 'class-variance-authority'

/**
 * Single source of truth for form-control chrome — Input, Textarea, SearchField
 * and select triggers all consume this. Padding-driven sizing (no fixed
 * heights), 2.5px radius, 12px text. The visual chrome (border alpha 7% → 14%
 * → full, inset top shadow removed on focus) comes from the `.pb-input-chrome`
 * rule in styles.css so every control shares one exact look.
 */
export const controlVariants = cva(
  'pb-input-chrome w-full min-w-0 rounded-control border text-pb-caption text-content-primary outline-none placeholder:text-content-tertiary disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50',
  {
    variants: {
      size: {
        xs: 'px-2 py-0.5 text-pb-micro',
        sm: 'px-2 py-1',
        default: 'px-2.5 py-1.5',
        lg: 'px-3 py-2 text-pb-body',
      },
      /**
       * `code` = character-by-character entry (verification codes, short ids):
       * mono face, centred, wide tracking. Use it instead of re-specifying the
       * font/size on an `Input` at the call site.
       */
      face: {
        default: '',
        code: 'text-center font-pbmono text-pb-body tracking-[0.35em]',
      },
    },
    defaultVariants: {
      size: 'default',
      face: 'default',
    },
  },
)

export type ControlVariantProps = VariantProps<typeof controlVariants>
