import { ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from './Button'
import { cx } from './cx'

/** Pages of a long list: which rows are shown and the two steps. Nothing when everything fits on one page. Sits at the
 *  bottom of a list frame (a line above it); `className` gives it a frame of its own under a list of cards. */
export function Pagination({ page, perPage, total, onPage, className }: { page: number; perPage: number; total: number; onPage: (page: number) => void; className?: string }) {
  const pages = Math.max(1, Math.ceil(total / perPage))
  if (pages <= 1) return null
  return (
    <div className={cx('flex items-center justify-between gap-3 border-t border-line px-4 py-2.5 text-caption text-ink-3 sm:px-5', className)}>
      <span className="tabular-nums">
        {(page * perPage + 1).toLocaleString('ru-RU')}–{Math.min((page + 1) * perPage, total).toLocaleString('ru-RU')} из {total.toLocaleString('ru-RU')}
      </span>
      <div className="flex gap-1">
        <Button variant="ghost" size="sm" disabled={page === 0} onClick={() => onPage(page - 1)}>
          <ChevronLeft aria-hidden />
          Назад
        </Button>
        <Button variant="ghost" size="sm" disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>
          Вперёд
          <ChevronRight aria-hidden />
        </Button>
      </div>
    </div>
  )
}
