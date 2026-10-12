import * as React from 'react'

import { cn } from './cn'
import { SearchGlyph, XGlyph } from './glyphs'

export interface SearchFieldProps extends Omit<React.ComponentProps<'input'>, 'onChange' | 'size'> {
  value: string
  onChange: (value: string) => void
  /** Placeholder text; defaults to "Search". */
  placeholder?: string
}

/**
 * The only search input. Borderless with an underline that carries focus,
 * leading search glyph, clear affordance when non-empty. Don't build boxed
 * search bars; don't wrap it in a bordered tile. Empty lists hide their
 * search field entirely.
 */
export function SearchField({ className, onChange, placeholder = 'Search', value, ...props }: SearchFieldProps) {
  return (
    <div
      className={cn(
        'group flex w-full min-w-0 items-center gap-1.5 border-b border-stroke-tertiary pb-1 text-content-secondary transition-colors duration-100',
        'focus-within:border-accent focus-within:text-content-primary',
        className,
      )}
      data-slot="search-field"
    >
      <SearchGlyph className="size-3.5 shrink-0 text-content-quaternary group-focus-within:text-content-secondary" />
      {/* type="text" + role="searchbox": `type="search"` paints WebKit's own
          bold cancel ✕ right next to ours, so every search field showed two
          clear buttons. The primitive's clear button is the only one. */}
      <input
        className="w-full min-w-0 bg-transparent text-pb-caption text-content-primary outline-none placeholder:text-content-tertiary"
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        role="searchbox"
        type="text"
        value={value}
        {...props}
      />
      {value !== '' ? (
        <button
          aria-label="Clear search"
          className="cursor-pointer rounded-iconbtn p-0.5 text-content-quaternary hover:bg-control-hover hover:text-content-secondary"
          onClick={() => onChange('')}
          type="button"
        >
          <XGlyph className="size-3" />
        </button>
      ) : null}
    </div>
  )
}
