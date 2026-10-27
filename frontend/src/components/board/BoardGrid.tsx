import { ListChecks, Maximize2 } from 'lucide-react'
import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import { Count } from '../ui/Badge'
import { Button } from '../ui/Button'
import { cx } from '../ui/cx'

export type OpenColumn = (key: string, band?: string, from?: Element | null) => void

/** A column as the board and the narrow columns around an open one show it. */
export interface GridColumn<I> {
  key: string
  title: string
  count: number
  items: I[]
}

/** A card on the board, whatever the pipeline: what it is, its score and the step it waits at (or a badge); a click
 *  opens its side card. */
export function CardShell({ title, subtitle, score, stage, badge, onClick }: { title: string; subtitle: string; score: ReactNode; stage?: string; badge?: ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full rounded-lg border border-line bg-surface px-3 py-2.5 text-left transition-[border-color,box-shadow] duration-150 hover:border-line-strong hover:shadow-[0_6px_16px_-10px_oklch(24.5%_0.022_264/0.35)] focus-visible:outline-2 focus-visible:outline-focus"
    >
      <span className="line-clamp-2 text-sm font-medium leading-snug text-ink">{title}</span>
      <span className="mt-0.5 block truncate text-xs text-ink-3">{subtitle}</span>
      <span className="mt-2 flex min-w-0 items-center gap-2">
        {score}
        {badge ?? <span className="min-w-0 text-xs leading-snug text-ink-3">{stage}</span>}
      </span>
    </button>
  )
}

export function Tile({ label, count, tone, dashed, onClick }: { label: string; count: number; tone?: 'good' | 'warn'; dashed?: boolean; onClick: (el: HTMLElement) => void }) {
  return (
    <button
      type="button"
      onClick={(e) => onClick(e.currentTarget)}
      className={cx(
        'flex w-full items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-caption transition-[border-color] duration-150 hover:border-line-strong focus-visible:outline-2 focus-visible:outline-focus',
        dashed ? 'border-dashed border-line-strong text-ink-3' : 'border-line bg-surface text-ink-2',
      )}
    >
      <span className="min-w-0 leading-snug">{label}</span>
      <b className={cx('text-lg font-semibold tabular-nums tracking-[-0.01em]', tone === 'good' ? 'text-good' : tone === 'warn' ? 'text-warn' : dashed ? 'text-ink-3' : 'text-ink')}>{count.toLocaleString('ru-RU')}</b>
    </button>
  )
}

export interface SummaryTile {
  band: string
  label: string
  count: number
  tone?: 'good' | 'warn'
  dashed?: boolean
}

/** The first column of a board as a summary: hundreds of new vacancies (or studios) would not fit a column, so its
 *  bands as tiles, the button that starts working through them, and the best cards. */
export function ColumnSummary({
  fresh,
  tiles,
  startLabel,
  startBand,
  startDisabled,
  bestTitle,
  best,
  foot,
  onOpen,
}: {
  fresh?: number
  tiles: SummaryTile[]
  startLabel: string
  startBand: string
  startDisabled: boolean
  bestTitle: string
  best: ReactNode[]
  foot?: ReactNode
  onOpen: OpenColumn
}) {
  return (
    <div className="flex flex-col gap-1.5">
      {fresh !== undefined && fresh > 0 && <span className="self-start rounded-full bg-accent-soft px-2.5 py-0.5 text-xs font-medium text-accent-strong">Сегодня новых: {fresh}</span>}
      {tiles.map((t) => (
        <Tile key={t.band} label={t.label} count={t.count} tone={t.tone} dashed={t.dashed} onClick={(el) => onOpen('new', t.band, el.closest('section'))} />
      ))}
      <Button variant="primary" className="mt-0.5 w-full" disabled={startDisabled} onClick={(e) => onOpen('new', startBand, e.currentTarget.closest('section'))}>
        <ListChecks aria-hidden />
        {startLabel}
      </Button>
      {best.length > 0 && <div className="mt-2 px-1 text-xs font-medium text-ink-3">{bestTitle}</div>}
      {best}
      {foot}
    </div>
  )
}

function ColumnBox<I>({ column, onOpen, children }: { column: GridColumn<I>; onOpen: OpenColumn; children: ReactNode }) {
  const ref = useRef<HTMLElement>(null)
  return (
    <section ref={ref} aria-labelledby={`board-col-${column.key}`} className="flex snap-start flex-col gap-2 rounded-xl bg-sunken p-2">
      <div className="flex min-h-7 items-center justify-between gap-1 pl-1.5">
        <h2 id={`board-col-${column.key}`} className="min-w-0 text-sm font-semibold leading-tight text-ink">
          {column.title}
        </h2>
        <span className="flex items-center gap-0.5">
          <Count value={column.count} />
          {column.count > 0 && (
            <Button variant="ghost" size="sm" iconOnly aria-label={`Раскрыть колонку «${column.title}»`} title="Раскрыть колонку" onClick={() => onOpen(column.key, undefined, ref.current)}>
              <Maximize2 aria-hidden />
            </Button>
          )}
        </span>
      </div>
      {children}
    </section>
  )
}

/** The whole board: the first column as the source's summary (`first`), the other columns with their first cards. A
 *  phone slides the columns by a finger, one at a time, with the edge of the next one in sight and dots under the
 *  funnel. */
export default function BoardGrid<I>({
  columns,
  narrow,
  onOpen,
  itemKey,
  renderCard,
  first,
}: {
  columns: GridColumn<I>[]
  narrow: boolean
  onOpen: OpenColumn
  itemKey: (item: I) => number
  renderCard: (item: I, col: string) => ReactNode
  first?: ReactNode
}) {
  const scroller = useRef<HTMLDivElement>(null)
  const [shown, setShown] = useState(0)
  useEffect(() => {
    const el = scroller.current
    if (!el || !narrow) return
    const onScroll = () => {
      const firstColumn = el.firstElementChild as HTMLElement | null
      const step = (firstColumn?.getBoundingClientRect().width ?? 1) + 10
      setShown(Math.max(0, Math.min(columns.length - 1, Math.round(el.scrollLeft / step))))
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [narrow, columns.length])

  return (
    <div className="flex flex-col gap-2">
      {narrow && (
        <div className="flex items-center justify-center gap-1.5 text-xs text-ink-2" aria-hidden>
          {columns.map((c, i) => (
            <span key={c.key} className={cx('h-1.5 rounded-full transition-[width,background-color] duration-200', i === shown ? 'w-4 bg-ink' : 'w-1.5 bg-line-strong')} />
          ))}
          <span className="ml-1.5">
            {columns[shown]?.title} · {columns[shown]?.count}
          </span>
        </div>
      )}
      <div
        ref={scroller}
        className={cx(
          'grid grid-flow-col gap-2.5 overflow-x-auto pb-1',
          narrow ? 'snap-x snap-mandatory auto-cols-[86%] scroll-px-4 [scrollbar-width:none]' : 'auto-cols-[minmax(11.5rem,1fr)]',
        )}
      >
        {columns.map((column, i) => (
          <ColumnBox key={column.key} column={column} onOpen={onOpen}>
            {i === 0 && first !== undefined ? (
              first
            ) : (
              <>
                {column.items.map((item) => (
                  <Fragment key={itemKey(item)}>{renderCard(item, column.key)}</Fragment>
                ))}
                {column.items.length === 0 && <div className="px-1.5 pb-1 text-caption text-ink-3">Пока пусто</div>}
                {column.count > column.items.length && (
                  <button
                    type="button"
                    onClick={(e) => onOpen(column.key, undefined, e.currentTarget.closest('section'))}
                    className="rounded-md px-2 py-1.5 text-left text-caption font-medium text-accent hover:bg-surface focus-visible:outline-2 focus-visible:outline-focus"
                  >
                    Ещё {column.count - column.items.length} · раскрыть
                  </button>
                )}
              </>
            )}
          </ColumnBox>
        ))}
      </div>
    </div>
  )
}
