import { X } from 'lucide-react'
import type { ReactNode } from 'react'
import { cx } from './cx'

/** An active filter: what hides part of the list, removable with its cross. */
export function Chip({ children, onRemove, removeLabel }: { children: ReactNode; onRemove: () => void; removeLabel: string }) {
  return (
    <span className="inline-flex h-7 max-w-full items-center gap-0.5 rounded-full bg-sunken pl-2.5 pr-0.5 text-caption text-ink">
      <span className="truncate">{children}</span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={removeLabel}
        title={removeLabel}
        className="grid size-6 shrink-0 place-items-center rounded-full text-ink-3 transition-colors hover:bg-selected hover:text-ink focus-visible:outline-2 focus-visible:outline-focus"
      >
        <X className="size-3.5" aria-hidden />
      </button>
    </span>
  )
}

/** A multi-select option (experience levels and the like): pressed is accent-tinted. */
export function ToggleChip({ pressed, onToggle, children }: { pressed: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onToggle}
      className={cx(
        'inline-flex h-7 items-center whitespace-nowrap rounded-full border px-2.5 text-caption transition-colors',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus pointer-coarse:h-9',
        pressed ? 'border-accent bg-accent-soft text-accent-strong' : 'border-line-strong text-ink-2 hover:bg-sunken hover:text-ink',
      )}
    >
      {children}
    </button>
  )
}
