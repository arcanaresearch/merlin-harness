import * as TabsPrimitive from '@radix-ui/react-tabs'
import * as React from 'react'

import { cn } from './cn'

/**
 * Tabs. Underline style: quiet labels on a hairline track; the active tab is
 * text-primary with a 2px accent underline. For small mutually-exclusive
 * choices that AREN'T navigation, use SegmentedControl instead.
 */

const Tabs = TabsPrimitive.Root

function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn('flex items-center gap-3 border-b border-stroke-tertiary', className)}
      data-slot="tabs-list"
      {...props}
    />
  )
}

function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        '-mb-px cursor-pointer select-none border-b-2 border-transparent px-1 pb-1.5 text-pb-caption font-medium text-content-tertiary transition-colors duration-100',
        'hover:text-content-primary data-[state=active]:border-accent data-[state=active]:text-content-primary',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--ui-focus-ring)',
        className,
      )}
      data-slot="tabs-trigger"
      {...props}
    />
  )
}

/** Count inside a tab label. The ONE way to show "how many" on a tab —
 *  `Findings <TabsTriggerMeta>7</TabsTriggerMeta>`, never "Findings (7)". */
function TabsTriggerMeta({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      // Secondary ink. A live count is normal-size readable text, so it owes
      // 4.5:1 — quaternary measured 2.64:1 light / 3.88:1 dark, and tertiary is
      // still only 3.49:1 on the light page. Secondary clears it in both
      // themes (6.46:1 / 8.72:1). Quaternary stays reserved for disabled
      // glyphs and dot fills.
      className={cn('ml-1 text-[0.85em] font-normal text-content-secondary tabular-nums', className)}
      data-slot="tabs-trigger-meta"
      {...props}
    />
  )
}

function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content className={cn('min-w-0 outline-none', className)} data-slot="tabs-content" {...props} />
}

export { Tabs, TabsContent, TabsList, TabsTrigger, TabsTriggerMeta }
