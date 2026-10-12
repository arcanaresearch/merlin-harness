/**
 * Pirateboys desktop renderer — primitive library barrel.
 *
 * Import path:
 *   import { Button, Input, Tip, … } from '@/ui'
 *
 * One primitive per concern. If a screen needs something not here, add the
 * primitive here (and its DESIGN.md entry) — never fork one at the call site.
 */

export { cn } from './cn'

export { Button, buttonVariants, type ButtonProps } from './button'
export { controlVariants, type ControlVariantProps } from './control'
export { Input, type InputProps } from './input'
export { Field, Textarea, type FieldProps, type TextareaProps } from './field'
export { SearchField, type SearchFieldProps } from './search-field'
export { SegmentedControl, type SegmentedControlOption, type SegmentedControlProps } from './segmented-control'
export { Switch, type SwitchProps } from './switch'

export {
  OverflowTip,
  RootTooltipProvider,
  Tip,
  TipHintLabel,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
  type OverflowTipProps,
  type TipProps,
} from './tooltip'

export {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuPortal,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
  DropdownMenuTriggerCaret,
  type DropdownMenuItemVariant,
} from './dropdown-menu'

export {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
  usePointerQuiet,
  type CommandInputProps,
} from './command'

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
  type DialogContentProps,
} from './dialog'
export { PortalContainerProvider, usePortalContainer } from './portal-container'

export { ToastProvider, ToastStacks, useToasts, type ToastAction, type ToastItem, type ToastKind } from './notifications'

export { Alert, type AlertProps, type AlertTone } from './alert'
export { BrandMark, type BrandMarkProps } from './brand-mark'
export { Chip, CodeBlock, type ChipProps, type CodeBlockProps } from './chip'
export { CopyButton, writeClipboardText, type CopyButtonProps } from './copy-button'
export { Disclosure, type DisclosureProps } from './disclosure'
export { EmptyState, type EmptyStateProps } from './empty-state'
export { ErrorState, type ErrorStateProps } from './error-state'
export { Loader, type LoaderProps } from './loader'
export { PageHeader, type PageHeaderProps } from './page-header'
export { Progress, type ProgressProps } from './progress'
export { SectionLabel, type SectionLabelProps } from './section-label'
export { Skeleton, type SkeletonProps } from './skeleton'
export { Tabs, TabsContent, TabsList, TabsTrigger, TabsTriggerMeta } from './tabs'
export { Kbd, KbdGroup, type KbdGroupProps, type KbdProps } from './kbd'
export {
  Panel,
  PanelBody,
  PanelDetail,
  PanelEmpty,
  PanelHeader,
  PanelList,
  PanelListRow,
  PanelPill,
  PanelRowKebab,
  type PanelDetailProps,
  type PanelListRowProps,
  type PanelPillProps,
} from './panel'
export { StatusDot, type StatusDotProps } from './status-dot'

export * from './glyphs'
