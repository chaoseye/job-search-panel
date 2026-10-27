import type { ReactNode } from 'react'
import { cx } from './cx'

export type BadgeTone = 'neutral' | 'good' | 'warn' | 'bad' | 'info' | 'outline'

const TONES: Record<BadgeTone, string> = {
  neutral: 'bg-sunken text-ink-2',
  good: 'bg-good-soft text-good',
  warn: 'bg-warn-soft text-warn',
  bad: 'bg-bad-soft text-bad',
  info: 'bg-accent-soft text-accent-strong',
  outline: 'border border-line text-ink-2',
}

/** Read-only status label: a small square-cornered tag (filters and toggles are round chips, see Chip). */
export function Badge({ tone = 'neutral', children, title, className }: { tone?: BadgeTone; children: ReactNode; title?: string; className?: string }) {
  return (
    <span title={title} className={cx('inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded-sm px-1.5 text-xs font-medium', TONES[tone], className)}>
      {children}
    </span>
  )
}

/** Count next to a navigation item or a tab: quiet grey, or accent when the queue waits for the user. */
export function Count({ value, attention = false, className }: { value: number; attention?: boolean; className?: string }) {
  return (
    <span
      className={cx(
        'inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-medium tabular-nums',
        attention ? 'bg-accent-soft text-accent-strong' : 'text-ink-3',
        className,
      )}
    >
      {value > 999 ? '999+' : value}
    </span>
  )
}
