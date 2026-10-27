import type { BoardFunnelStep } from '../../api/types'
import { funnelCells } from '../../lib/board'
import { cx } from '../ui/cx'

/** The funnel over the board (30 days of vacancies or tenders, the studios in work): each step with its share of the
 *  step before, the weakest transition tinted (there the search loses the most). A phone scrolls the steps sideways,
 *  each as wide as its words. */
export default function FunnelStrip({ steps, label }: { steps: BoardFunnelStep[]; label: string }) {
  const cells = funnelCells(steps)
  return (
    <div className="flex overflow-x-auto rounded-lg border border-line bg-surface [scrollbar-width:none]" role="list" aria-label={label}>
      {cells.map((c, i) => (
        <div
          key={c.key}
          role="listitem"
          className={cx('relative min-w-[6.5rem] flex-1 px-3 py-2 max-md:flex-none', i > 0 && 'border-l border-line', c.weak && 'bg-bad-soft')}
          title={[c.hint, c.weak ? 'Самый слабый переход' : null].filter(Boolean).join('. ') || undefined}
        >
          <div className="text-lg font-semibold tabular-nums tracking-[-0.01em] text-ink">{c.value.toLocaleString('ru-RU')}</div>
          <div className="truncate text-xs text-ink-3">{c.label}</div>
          {c.conv !== null && (
            <span className={cx('absolute right-2.5 top-2 text-xs tabular-nums', c.weak ? 'font-semibold text-bad' : 'text-ink-3')} aria-label={`${c.conv}% от прошлого шага`}>
              {c.conv}%
            </span>
          )}
        </div>
      ))}
    </div>
  )
}
