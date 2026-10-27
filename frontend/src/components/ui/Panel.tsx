import { useId, type ReactNode } from 'react'
import { cx } from './cx'

/** A block of a page on a white surface: a title (the h2 of the block), a line about it, its actions on the right.
 *  `flush` leaves the body without side padding, for a table or a list that runs edge to edge. */
export function Panel({
  title,
  meta,
  actions,
  children,
  className,
  bodyClassName,
  flush = false,
}: {
  title?: ReactNode
  meta?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  flush?: boolean
}) {
  const id = useId()
  const head = title !== undefined || actions !== undefined
  return (
    <section aria-labelledby={title !== undefined ? id : undefined} className={cx('min-w-0 rounded-lg border border-line bg-surface', className)}>
      {head && (
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 px-4 pt-4 sm:px-5">
          <div className="min-w-0 max-w-3xl">
            {title !== undefined && (
              <h2 id={id} className="text-[15px] font-semibold text-ink">
                {title}
              </h2>
            )}
            {meta && <div className="mt-0.5 text-caption text-ink-3">{meta}</div>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={cx(flush ? (head ? 'mt-3' : '') : cx('px-4 pb-4 sm:px-5 sm:pb-5', head ? 'pt-3' : 'pt-4 sm:pt-5'), bodyClassName)}>{children}</div>
    </section>
  )
}
