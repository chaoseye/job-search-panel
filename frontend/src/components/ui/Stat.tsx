import { Info } from 'lucide-react'
import type { ReactNode } from 'react'
import { cx } from './cx'

/** One number of a summary: what it counts, the value, a line under it; the definition sits in the tooltip. `good`
 *  marks the outcome the whole search is for (invitations). */
export function Stat({ label, value, sub, hint, tone }: { label: ReactNode; value: ReactNode; sub?: ReactNode; hint?: string; tone?: 'good' }) {
  return (
    <div className={cx('min-w-0 rounded-lg border px-4 py-3', tone === 'good' ? 'border-good/25 bg-good-soft' : 'border-line bg-surface')} title={hint}>
      <div className="flex items-start gap-1 text-caption text-ink-2">
        <span className="min-w-0">{label}</span>
        {hint && <Info aria-hidden className="mt-0.5 size-3.5 shrink-0 text-ink-3" />}
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums tracking-[-0.01em] text-ink">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-ink-3">{sub}</div>}
    </div>
  )
}
