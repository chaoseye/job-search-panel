import { ChevronDown } from 'lucide-react'
import type { ReactNode } from 'react'
import { cx } from './cx'

/** Text that most of the time stays folded («Как это работает», a long list): the native details element, so the
 *  keyboard and screen readers get it for free; the chevron turns when it opens. */
export function Disclosure({ summary, children, className, defaultOpen }: { summary: ReactNode; children: ReactNode; className?: string; defaultOpen?: boolean }) {
  return (
    <details className={cx('group/disclosure', className)} open={defaultOpen}>
      <summary className="flex w-fit cursor-pointer list-none items-center gap-1 rounded text-caption font-medium text-ink [&::-webkit-details-marker]:hidden">
        <ChevronDown aria-hidden className="size-4 -rotate-90 text-ink-3 transition-transform duration-150 group-open/disclosure:rotate-0 motion-reduce:transition-none" />
        {summary}
      </summary>
      <div className="mt-2">{children}</div>
    </details>
  )
}
