import * as React from 'react'

import { cn } from './cn'
import { controlVariants, type ControlVariantProps } from './control'

export interface InputProps extends Omit<React.ComponentProps<'input'>, 'size'>, ControlVariantProps {}

/** The single text input. Chrome comes from `controlVariants`; do not wrap it
 *  in a bordered tile or restyle padding/radius/font per call site — use the
 *  `size` and `face` variants instead. */
export const Input = React.forwardRef<HTMLInputElement, InputProps>(({ className, face, size, type = 'text', ...props }, ref) => (
  <input className={cn(controlVariants({ face, size }), className)} data-slot="input" ref={ref} type={type} {...props} />
))
Input.displayName = 'Input'
