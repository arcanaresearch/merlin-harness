import * as React from 'react'

import { cn } from './cn'
import { controlVariants, type ControlVariantProps } from './control'

export interface FieldProps {
  /** Sentence-case label. Says what the value IS, not what to do. */
  label: React.ReactNode
  /** One line of guidance under the control. */
  hint?: React.ReactNode
  /** Validation message. Replaces the hint and colours the block. */
  error?: React.ReactNode
  /** Ties the label to the control. Pass the control's `id`. */
  htmlFor?: string
  children: React.ReactNode
  className?: string
}

/**
 * The ONE label + control + hint/error stack. Every form uses it, so labels
 * share one size, one colour and one spacing — never hand-roll
 * `<label><span className="text-[11px]…">`.
 */
export function Field({ children, className, error, hint, htmlFor, label }: FieldProps) {
  return (
    <div className={cn('flex flex-col gap-1', className)} data-slot="field">
      <label className="text-pb-caption font-medium text-content-secondary" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error !== undefined && error !== null && error !== '' ? (
        <p className="pb-selectable text-pb-caption text-sev-red" data-slot="field-error" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p className="text-pb-caption text-content-tertiary" data-slot="field-hint">
          {hint}
        </p>
      ) : null}
    </div>
  )
}

export interface TextareaProps extends Omit<React.ComponentProps<'textarea'>, 'size'>, ControlVariantProps {}

/**
 * The single multi-line text control. Same chrome as `Input` (via
 * `controlVariants`), so a textarea never looks like a different design system
 * from the field above it. Resize is off — grow it with `rows` or a
 * call-site auto-grow effect, not a drag handle.
 */
export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(({ className, face, size, ...props }, ref) => (
  <textarea className={cn(controlVariants({ face, size }), 'resize-none', className)} data-slot="textarea" ref={ref} {...props} />
))
Textarea.displayName = 'Textarea'
