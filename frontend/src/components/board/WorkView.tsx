import { ChevronLeft, ChevronRight, Minimize2 } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, type MutableRefObject, type ReactNode } from 'react'
import { countOf } from '../../lib/format'
import { Count } from '../ui/Badge'
import { Button } from '../ui/Button'
import { cx } from '../ui/cx'
import { prefersReducedMotion } from '../ui/floating'
import { Segmented } from '../ui/Segmented'
import { EmptyState, ErrorNotice, Skeleton } from '../ui/States'
import type { GridColumn } from './BoardGrid'

export interface BandChip {
  band: string
  label: string
  count: number
}

/** What a row of the open list gets from the view: its place, whether it is the focused one, how to focus it. */
export interface RowSlot {
  index: number
  focused: boolean
  onFocus: () => void
}

/** The query of the open column, as much of it as the view reads. */
export interface ListState<I> {
  data?: { items: I[]; total: number }
  isLoading: boolean
  isFetching: boolean
  error: unknown
  refetch: () => unknown
}

/** A narrow column next to the open one; a click opens it. A wide board shows its count, what it holds in words
 *  (`extra`) and its first cards; a laptop narrows it to a rail with the count and the title down the side, so the
 *  open column keeps the width its details need. */
function MiniColumn<I>({
  column,
  extra,
  itemKey,
  itemTitle,
  arrived,
  onOpen,
}: {
  column: GridColumn<I>
  extra?: ReactNode
  itemKey: (item: I) => number
  itemTitle: (item: I) => string
  arrived: boolean
  onOpen: (el: HTMLElement) => void
}) {
  return (
    <button
      type="button"
      onClick={(e) => onOpen(e.currentTarget)}
      aria-label={`Раскрыть колонку «${column.title}», ${column.count}`}
      title={`${column.title}: ${column.count}`}
      className={cx(
        'flex w-14 shrink-0 flex-col items-center rounded-xl border border-transparent bg-sunken px-1 py-3 text-left transition-[background-color,border-color] duration-150 @7xl/work:w-[8.5rem] @7xl/work:items-stretch @7xl/work:p-2.5',
        'hover:border-line hover:bg-selected focus-visible:outline-2 focus-visible:outline-focus',
        arrived && 'motion-safe:animate-arrive',
      )}
    >
      <span className="flex flex-col items-center gap-2.5 @7xl/work:hidden">
        <span className="text-[15px] font-semibold tabular-nums text-ink">{column.count.toLocaleString('ru-RU')}</span>
        <span className="text-caption font-semibold whitespace-nowrap text-ink-2 [writing-mode:vertical-rl]">{column.title}</span>
      </span>
      <span className="hidden min-h-0 flex-1 flex-col @7xl/work:flex">
        <span className="text-caption font-semibold leading-tight text-ink">{column.title}</span>
        <span className="mt-1 text-2xl font-semibold tabular-nums tracking-[-0.02em] text-ink">{column.count}</span>
        {extra && <span className="mt-1.5 flex flex-col gap-px text-xs text-ink-3">{extra}</span>}
        <span className="mt-2.5 flex min-h-0 flex-col gap-1.5 overflow-hidden">
          {column.items.map((it) => (
            <span key={itemKey(it)} className="line-clamp-2 border-t border-line pt-1.5 text-xs leading-snug text-ink-2">
              {itemTitle(it)}
            </span>
          ))}
        </span>
        <span className="mt-auto pt-2 text-xs font-medium text-accent">Раскрыть</span>
      </span>
    </button>
  )
}

/** The open column (Б3 inside the board of Б), whatever the pipeline: one column as a list with the details under
 *  the focused card, the others narrowed around it. ←/→ open the neighbours, Esc or «Вся доска» brings the board back.
 *  A phone shows the open column alone, the neighbours behind the arrows in its head. */
export default function WorkView<I>({
  columns,
  col,
  chips,
  chipsLabel,
  band,
  list,
  itemKey,
  itemTitle,
  renderRow,
  miniExtra,
  empty,
  keyHint,
  narrow,
  arrived,
  focus,
  growFrom,
  onFocus,
  onSwitch,
  onBand,
  onClose,
  onMore,
}: {
  columns: GridColumn<I>[]
  col: string
  chips: BandChip[]
  chipsLabel: string
  band: string | null
  list: ListState<I>
  itemKey: (item: I) => number
  itemTitle: (item: I) => string
  renderRow: (item: I, slot: RowSlot) => ReactNode
  miniExtra?: (column: GridColumn<I>) => ReactNode
  empty: { title: string; body: string; action?: ReactNode }
  keyHint: string
  narrow: boolean
  arrived: string | null
  focus: number
  growFrom: MutableRefObject<DOMRect | null>
  onFocus: (index: number) => void
  onSwitch: (key: string, from?: Element | null) => void
  onBand: (band: string) => void
  onClose: () => void
  onMore: () => void
}) {
  const index = columns.findIndex((c) => c.key === col)
  const column = columns[index]
  const items = list.data?.items ?? []
  const total = list.data?.total ?? 0
  const active = useRef<HTMLElement>(null)
  const scroller = useRef<HTMLUListElement>(null)
  const prev = columns[index - 1] ?? null
  const next = columns[index + 1] ?? null

  // the open column grows out of the place it was clicked in (a column of the board, a narrow one); keys open it at once
  useLayoutEffect(() => {
    const from = growFrom.current
    growFrom.current = null
    const el = active.current
    if (!from || !el || narrow || prefersReducedMotion() || typeof el.animate !== 'function') return
    const to = el.getBoundingClientRect()
    const inset = [from.top - to.top, to.right - from.right, to.bottom - from.bottom, from.left - to.left].map((v) => `${Math.max(0, Math.round(v))}px`).join(' ')
    el.animate([{ clipPath: `inset(${inset} round 12px)` }, { clipPath: 'inset(0px 0px 0px 0px round 12px)' }], { duration: 260, easing: 'cubic-bezier(0.23, 1, 0.32, 1)' })
  }, [col, growFrom, narrow])

  // the focused card stays in sight: the list scrolls, the page does not (scrollIntoView would move the page too)
  const focusedId = items[focus] === undefined ? undefined : itemKey(items[focus])
  useEffect(() => {
    const box = scroller.current
    const row = focusedId === undefined ? null : box?.querySelector<HTMLElement>(`[data-board-row="${focusedId}"]`)
    if (!box || !row) return
    const top = row.offsetTop
    const bottom = top + row.offsetHeight
    if (top < box.scrollTop) box.scrollTop = top - 4
    else if (bottom > box.scrollTop + box.clientHeight) box.scrollTop = bottom - box.clientHeight + 4
  }, [focusedId, focus])

  if (!column) return null
  const mini = (c: GridColumn<I>) => (
    <MiniColumn key={c.key} column={c} extra={miniExtra?.(c)} itemKey={itemKey} itemTitle={itemTitle} arrived={arrived === c.key} onOpen={(el) => onSwitch(c.key, el)} />
  )

  return (
    <div className={cx('@container/work flex gap-2', narrow ? 'h-[calc(100dvh-13rem)] min-h-[26rem]' : 'h-[clamp(30rem,calc(100dvh-14rem),58rem)]')}>
      {!narrow && columns.slice(0, index).map(mini)}

      <section ref={active} aria-labelledby="board-open-title" className="@container flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-line bg-surface">
        <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-3 py-2.5">
          <Button size="sm" iconOnly={narrow} aria-label={narrow ? 'Вся доска' : undefined} onClick={onClose} title="Вернуть всю доску (Esc)">
            <Minimize2 aria-hidden />
            {!narrow && 'Вся доска'}
          </Button>
          <h2 id="board-open-title" className="flex min-w-0 items-center gap-1 text-[15px] font-semibold text-ink">
            {column.title}
            <Count value={column.count} />
          </h2>
          {narrow && (
            <span className="ml-auto flex items-center">
              <Button variant="ghost" iconOnly aria-label={prev ? `Открыть «${prev.title}»` : 'Это первая колонка'} disabled={!prev} onClick={() => prev && onSwitch(prev.key)}>
                <ChevronLeft aria-hidden />
              </Button>
              <Button variant="ghost" iconOnly aria-label={next ? `Открыть «${next.title}»` : 'Это последняя колонка'} disabled={!next} onClick={() => next && onSwitch(next.key)}>
                <ChevronRight aria-hidden />
              </Button>
            </span>
          )}
          {chips.length > 0 && band && (
            <Segmented
              label={chipsLabel}
              className={cx(narrow && 'basis-full flex-nowrap overflow-x-auto [scrollbar-width:none]')}
              options={chips.map((c) => [c.band, `${c.label} · ${c.count.toLocaleString('ru-RU')}`] as [string, string])}
              value={band}
              onChange={onBand}
            />
          )}
        </header>

        {list.isLoading && (
          <div className="space-y-3 p-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        )}
        {Boolean(list.error) && <ErrorNotice className="m-4" error={list.error} title="Не удалось загрузить колонку" onRetry={() => void list.refetch()} />}
        {list.data && items.length === 0 && (
          <EmptyState title={empty.title} action={empty.action}>
            {empty.body}
          </EmptyState>
        )}
        {items.length > 0 && (
          <ul ref={scroller} className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain" aria-label={column.title}>
            {items.map((item, i) => renderRow(item, { index: i, focused: i === focus, onFocus: () => onFocus(i) }))}
            {total > items.length && (
              <li className="p-3">
                <Button size="sm" variant="ghost" onClick={onMore} disabled={list.isFetching}>
                  Показать ещё · осталось {countOf(total - items.length, ['карточка', 'карточки', 'карточек'])}
                </Button>
              </li>
            )}
          </ul>
        )}
        {!narrow && <footer className="border-t border-line px-3 py-1.5 text-xs text-ink-3">{keyHint}</footer>}
      </section>

      {!narrow && columns.slice(index + 1).map(mini)}
    </div>
  )
}
