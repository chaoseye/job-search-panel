import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import type { ReactNode, TdHTMLAttributes, ThHTMLAttributes } from 'react'
import { cx } from './cx'

// The first and the last cell keep the panel's side padding, so a table inside a flush Panel lines up with its title.
const EDGE = 'first:pl-4 last:pr-4 sm:first:pl-5 sm:last:pr-5'

// Many numeric columns (segments, weekly trends): smaller type, tighter cells, numbers kept on one line and long
// headers («Google Sheets / Excel») wrapped instead. The cell selectors outweigh the cells' own utilities whatever
// their order in the stylesheet; the first and the last cell keep the panel's side padding (EDGE is more specific).
const COMPACT =
  'text-caption [&_th]:px-1.5 [&_th]:align-bottom [&_th]:whitespace-normal [&_td]:px-1.5 [&_td]:py-1.5 [&_td]:whitespace-nowrap'

/** A data table that scrolls sideways inside its block when the screen is narrower than `minWidth`. The scroller is
 *  positioned: an absolutely placed child (an sr-only header) would otherwise escape it and widen the whole page. */
export function Table({
  children,
  className,
  minWidth,
  label,
  density = 'default',
}: {
  children: ReactNode
  className?: string
  minWidth?: number
  label?: string
  density?: 'default' | 'compact'
}) {
  return (
    <div className="relative overflow-x-auto overscroll-x-contain">
      <table
        aria-label={label}
        className={cx('w-full border-collapse [&_tbody_tr:last-child>td]:border-b-0', density === 'compact' ? COMPACT : 'text-sm', className)}
        style={minWidth ? { minWidth } : undefined}
      >
        {children}
      </table>
    </div>
  )
}

export function Th({ children, align = 'left', className, ...rest }: ThHTMLAttributes<HTMLTableCellElement> & { align?: 'left' | 'right' }) {
  return (
    <th
      scope="col"
      className={cx('whitespace-nowrap border-b border-line px-3 py-2 text-xs font-medium text-ink-3', EDGE, align === 'right' ? 'text-right' : 'text-left', className)}
      {...rest}
    >
      {children}
    </th>
  )
}

export function Td({ children, align = 'left', className, ...rest }: TdHTMLAttributes<HTMLTableCellElement> & { align?: 'left' | 'right' }) {
  return (
    <td className={cx('border-b border-rule px-3 py-2.5 align-top', EDGE, align === 'right' && 'text-right tabular-nums', className)} {...rest}>
      {children}
    </td>
  )
}

/** A column header that sorts the table: the arrow shows the direction, the name says it to a screen reader. */
export function SortTh({
  id,
  label,
  sort,
  order,
  onSort,
  align = 'left',
}: {
  id: string
  label: string
  sort: string
  order: 'asc' | 'desc'
  onSort: (id: string) => void
  align?: 'left' | 'right'
}) {
  const active = sort === id
  const Icon = active ? (order === 'asc' ? ArrowUp : ArrowDown) : ArrowUpDown
  return (
    <Th align={align} aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button
        type="button"
        onClick={() => onSort(id)}
        className={cx('-mx-1 inline-flex items-center gap-1 rounded px-1 transition-colors hover:text-ink', active && 'text-ink')}
        title={active ? (order === 'asc' ? 'Сейчас по возрастанию, нажмите для убывания' : 'Сейчас по убыванию, нажмите для возрастания') : 'Сортировать'}
      >
        {label}
        <Icon aria-hidden className={cx('size-3.5', active ? 'text-ink' : 'text-ink-3/60')} />
      </button>
    </Th>
  )
}
