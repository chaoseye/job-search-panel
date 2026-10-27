import type { ReactNode } from 'react'
import { cx } from './cx'

/** Title of a screen (the one h1), a line about its data, and its actions on the right. */
export function PageHeader({ title, meta, actions, className }: { title: ReactNode; meta?: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <div className={cx('flex flex-wrap items-start justify-between gap-x-6 gap-y-3', className)}>
      {/* a long description wraps within its measure and leaves the row to the actions */}
      <div className="min-w-0 max-w-3xl">
        {/* below 1024 px the top bar already names the screen: the h1 stays for screen readers */}
        <h1 className="text-2xl font-semibold tracking-[-0.01em] text-ink max-lg:sr-only">{title}</h1>
        {meta && <div className="text-caption text-ink-3 lg:mt-1">{meta}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}
