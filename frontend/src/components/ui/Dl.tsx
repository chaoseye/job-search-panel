import type { ReactNode } from 'react'
import { cx } from './cx'

/** Name–value rows (settings, the state of a job): the names in a quiet column on the left, one under another on a phone. */
export function Dl({ rows, className }: { rows: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={cx('grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[minmax(0,13rem)_minmax(0,1fr)]', className)}>
      {rows.map(([name, value], i) => (
        <div key={i} className="contents">
          <dt className="text-ink-3">{name}</dt>
          <dd className="min-w-0 text-ink max-sm:mb-2">{value}</dd>
        </div>
      ))}
    </dl>
  )
}
