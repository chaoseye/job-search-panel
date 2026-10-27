import { Clock } from 'lucide-react'
import type { ReactNode } from 'react'
import type { HiddenEmployer, SavedSearch, SelectionProfile, VacancyRow } from '../api/types'
import { formatDateTime, shortAgo, shortDate } from '../lib/format'
import { GROUPS, type Filters } from '../lib/vacancyFilters'
import {
  ATTENTION_GROUPS,
  EXPERIENCE_LEVELS,
  experienceShort,
  formatsShort,
  GROUP_LABEL,
  letterBadge,
  rowFlags,
  salaryNet,
  salaryPublished,
  SORTS,
  WORK_FORMATS,
} from '../lib/vacancyView'
import { Badge } from './ui/Badge'
import { Button } from './ui/Button'
import { Chip, ToggleChip } from './ui/Chip'
import { cx } from './ui/cx'
import { Checkbox, Field, Input, Select } from './ui/Field'
import { Pagination } from './ui/Pagination'
import { Panel } from './ui/Panel'
import { ScoreChip } from './ui/Score'
import { SearchInput } from './ui/SearchInput'
import { Skeleton } from './ui/States'
import { Table, Td, Th } from './ui/Table'

// The classic view of «Вакансии»: the structure of the page before the redesign (every group at once, all the filters
// open, a table of ten columns, the hidden employers under it) drawn with the design system. Same data and actions as
// the compact view; VacanciesPage owns the state and picks the view.

const SCORE_INPUT = /^(?:\d{0,2}|100)$/ // the API takes 0–100: a keystroke that leaves the range is not taken
const DIGITS = /^\d*$/ // salary: no sign, no fraction (the API answers 422 to those)

/** Every group of the list with its count, one of them chosen: a radio group like Segmented, but the buttons wrap
 *  over as many lines as they need. A queue that waits for the user has an accent count. */
export function GroupButtons({ value, onChange, count }: { value: string; onChange: (id: string) => void; count: (id: string) => number | undefined }) {
  return (
    <div role="radiogroup" aria-label="Группы вакансий" className="flex flex-wrap gap-1.5">
      {GROUPS.map(([id]) => {
        const on = value === id
        const n = count(id)
        const attention = ATTENTION_GROUPS.has(id) && (n ?? 0) > 0
        return (
          <button
            key={id}
            type="button"
            role="radio"
            aria-checked={on}
            onClick={() => onChange(id)}
            className={cx(
              'inline-flex h-8 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 text-sm transition-colors pointer-coarse:h-10',
              'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus',
              on ? 'border-accent bg-accent-soft font-medium text-accent-strong' : 'border-line-strong text-ink-2 hover:bg-sunken hover:text-ink',
            )}
          >
            {GROUP_LABEL[id] ?? id}
            {/* the space keeps the accessible name «Кандидаты 83» (a flex gap is not text) */}
            {n !== undefined && (
              <>
                {' '}
                <span
                  className={cx(
                    'tabular-nums',
                    attention
                      ? cx('inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-medium text-accent-strong', on ? 'bg-surface' : 'bg-accent-soft')
                      : cx('text-caption', on ? 'text-accent-strong' : 'text-ink-3'),
                  )}
                >
                  {n.toLocaleString('ru-RU')}
                </span>
              </>
            )}
          </button>
        )
      })}
    </div>
  )
}

/** The groups and every filter, open in one panel. «Сбросить фильтры» keeps the group and the sort order, as the chips
 *  of the compact view do. */
export function ClassicFilters({
  filters,
  set,
  onGroup,
  onReset,
  canReset,
  count,
  searches,
  sets,
  snoozed,
  summary,
}: {
  filters: Filters
  set: (patch: Partial<Filters>) => void
  onGroup: (id: string) => void
  onReset: () => void
  canReset: boolean
  count: (id: string) => number | undefined
  searches: SavedSearch[]
  sets: SelectionProfile[]
  snoozed: number
  summary?: ReactNode
}) {
  const chosen = filters.experience.split(',').filter(Boolean)
  return (
    <Panel bodyClassName="space-y-4">
      <GroupButtons value={filters.group} onChange={onGroup} count={count} />
      {/* six columns from 1280 px, as before the redesign (search, the three lists; experience, region, salary, score,
          sort); three on a narrower screen, where the menu takes the side */}
      <div className="grid items-start gap-3 md:grid-cols-3 xl:grid-cols-6">
        <Field label="Название или компания" className="md:col-span-3 xl:col-span-2">
          <SearchInput label="Поиск по названию или компании" placeholder="Например, аналитик" value={filters.q} onChange={(q) => set({ q })} className="w-full" />
        </Field>
        <Field label="Поиск">
          <Select value={filters.search_id} onChange={(e) => set({ search_id: e.target.value })}>
            <option value="">Все поиски</option>
            {searches.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
        </Field>
        {sets.length > 0 && (
          <Field label="Критерии">
            <Select value={filters.selection_profile_id} onChange={(e) => set({ selection_profile_id: e.target.value })}>
              <option value="">Любые</option>
              <option value="0">Основная анкета</option>
              {sets.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </Select>
          </Field>
        )}
        <Field label="Формат работы">
          <Select value={filters.work_format} onChange={(e) => set({ work_format: e.target.value })}>
            {WORK_FORMATS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <fieldset className="min-w-0 md:col-span-3 xl:col-span-2">
          <legend className="mb-1.5 text-caption font-medium text-ink-2">Требуемый опыт</legend>
          <div className="flex flex-wrap gap-1.5">
            {EXPERIENCE_LEVELS.map(([id, label]) => {
              const on = chosen.includes(id)
              return (
                <ToggleChip key={id} pressed={on} onToggle={() => set({ experience: (on ? chosen.filter((x) => x !== id) : [...chosen, id]).join(',') })}>
                  {label}
                </ToggleChip>
              )
            })}
          </div>
        </fieldset>
        <Field label="Регион">
          <Input value={filters.area} onChange={(e) => set({ area: e.target.value })} placeholder="Москва" />
        </Field>
        <Field label="На руки от, ₽">
          <Input
            type="number"
            inputMode="numeric"
            min={0}
            step={10000}
            value={filters.salary_from}
            onChange={(e) => DIGITS.test(e.target.value) && set({ salary_from: e.target.value })}
            placeholder="150000"
          />
        </Field>
        <Field label="Оценка от">
          <Input
            type="number"
            inputMode="numeric"
            min={0}
            max={100}
            value={filters.min_score}
            onChange={(e) => SCORE_INPUT.test(e.target.value) && set({ min_score: e.target.value })}
          />
        </Field>
        <Field label="Сортировка">
          <Select value={filters.sort} onChange={(e) => set({ sort: e.target.value })}>
            {SORTS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 md:col-span-3 xl:col-span-6">
          <Checkbox label="Только новые" checked={filters.unseen_only} onChange={(e) => set({ unseen_only: e.target.checked })} />
          <Checkbox label={`Показывать отложенные${snoozed ? ` (${snoozed})` : ''}`} checked={filters.include_snoozed} onChange={(e) => set({ include_snoozed: e.target.checked })} />
          <Button variant="ghost" size="sm" onClick={onReset} disabled={!canReset}>
            Сбросить фильтры
          </Button>
          {summary && (
            <span className="ml-auto text-caption tabular-nums text-ink-3" aria-live="polite">
              {summary}
            </span>
          )}
        </div>
      </div>
    </Panel>
  )
}

/** The list as a table of ten columns, as before the redesign; the row opens the card, the box selects it. */
export function ClassicTable({
  items,
  loading,
  notice,
  empty,
  checked,
  onToggle,
  onToggleAll,
  selected,
  onOpen,
  page,
  perPage,
  total,
  onPage,
}: {
  items: VacancyRow[]
  loading: boolean
  notice?: ReactNode
  empty?: ReactNode
  checked: number[]
  onToggle: (id: number) => void
  onToggleAll: (on: boolean) => void
  selected: number | null
  onOpen: (id: number) => void
  page: number
  perPage: number
  total: number
  onPage: (page: number) => void
}) {
  const allChecked = items.length > 0 && items.every((v) => checked.includes(v.id))
  return (
    <Panel flush>
      {notice}
      {loading || items.length > 0 ? (
        <Table label="Вакансии" minWidth={960}>
          <thead>
            <tr>
              <Th className="w-10">
                {items.length > 0 && (
                  <input
                    type="checkbox"
                    className="size-4 cursor-pointer accent-accent"
                    checked={allChecked}
                    onChange={(e) => onToggleAll(e.target.checked)}
                    aria-label="Выбрать все на странице"
                    title="Выбрать все на странице"
                  />
                )}
              </Th>
              <Th>Оценка</Th>
              <Th>Вакансия</Th>
              <Th>Компания</Th>
              <Th align="right">Зарплата</Th>
              <Th>
                {/* the one long header may wrap: the column stays as narrow as its places */}
                <span className="whitespace-normal">Регион · формат</span>
              </Th>
              <Th>Опыт</Th>
              <Th>Опубл.</Th>
              <Th align="right" title="Откликов на hh.ru">
                Откл.
              </Th>
              <Th>Статус</Th>
            </tr>
          </thead>
          <tbody aria-busy={loading || undefined}>
            {loading
              ? Array.from({ length: 6 }, (_, i) => <SkeletonRow key={i} />)
              : items.map((v) => <ClassicRow key={v.id} v={v} active={selected === v.id} checked={checked.includes(v.id)} onToggle={() => onToggle(v.id)} onOpen={() => onOpen(v.id)} />)}
          </tbody>
        </Table>
      ) : (
        empty
      )}
      <Pagination page={page} perPage={perPage} total={total} onPage={onPage} />
    </Panel>
  )
}

function ClassicRow({ v, active, checked, onToggle, onOpen }: { v: VacancyRow; active: boolean; checked: boolean; onToggle: () => void; onOpen: () => void }) {
  const net = salaryNet(v.salary)
  const published = salaryPublished(v.salary)
  const formats = formatsShort(v)
  const flags = rowFlags(v.flags)
  const letter = letterBadge(v.letter_status)
  const station = v.metro[0]
  const place = [v.address, v.metro.length ? `м. ${v.metro.join(', ')}` : ''].filter(Boolean).join(' · ')
  return (
    <tr onClick={onOpen} className={cx('cursor-pointer transition-colors', active ? 'bg-accent-soft' : 'hover:bg-sunken')}>
      <Td onClick={(e) => e.stopPropagation()}>
        <input type="checkbox" className="mt-1 size-4 cursor-pointer accent-accent" checked={checked} onChange={onToggle} aria-label={`Выбрать: ${v.name}`} />
      </Td>
      <Td>
        <ScoreChip score={v.score} prescore={v.prescore} />
      </Td>
      <Td>
        <div className="flex min-w-40 max-w-96 items-start gap-2">
          {v.is_new && (
            <span className="mt-[7px] size-1.5 shrink-0 rounded-full bg-accent" title="Новая, ещё не просмотрена">
              <span className="sr-only">Новая.</span>
            </span>
          )}
          <div className="min-w-0">
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                onOpen()
              }}
              className="rounded-sm text-left font-medium text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            >
              {v.name}
            </button>
            {(flags.length > 0 || v.duplicate_of_id !== null) && (
              <div className="mt-1 flex flex-wrap gap-1">
                {flags.map((f) => (
                  <Badge key={f.key} tone="warn">
                    {f.label}
                  </Badge>
                ))}
                {v.duplicate_of_id !== null && (
                  <Badge tone="outline" title="Повторная публикация вакансии, которая уже есть в базе">
                    дубль
                  </Badge>
                )}
              </div>
            )}
            {/* the searches that found it: two lines at most (no padding on the clamped block), all of them in the tooltip */}
            {v.sources.length > 0 && (
              <div className="mt-1 line-clamp-2 text-xs text-ink-3" title={v.sources.join(', ')}>
                {v.sources.join(', ')}
              </div>
            )}
          </div>
        </div>
      </Td>
      <Td>
        {/* what is around the title is a size smaller, as in the compact list: the title reads first, the table fits */}
        <div className="min-w-24 max-w-56 text-caption">
          <span className="text-ink">{v.employer.name ?? 'Работодатель не указан'}</span>
          {v.employer.accredited_it && (
            <span className="ml-1 text-xs font-medium text-ink-3" title="Аккредитованная IT-компания">
              IT
            </span>
          )}
        </div>
      </Td>
      <Td align="right" title={published ?? undefined}>
        {/* the amount on one line; the published range under it may wrap («до вычета») so the table fits 1280 px */}
        {net ? <div className="whitespace-nowrap font-medium text-ink">{net}</div> : <span className="text-ink-3">—</span>}
        {published && <div className="text-xs text-ink-3">{published}</div>}
      </Td>
      <Td>
        <div className="max-w-48 text-caption text-ink-2">
          <div className="text-ink">{v.area_name ?? '—'}</div>
          {formats && <div className="text-xs">{formats}</div>}
          {/* a long station or street must not widen the column: w-0 takes it out of the column's width, min-w-full
              gives it the width the column got, and the rest of the place is in the tooltip */}
          {place && (
            <div className="w-0 min-w-full truncate text-xs text-ink-3" title={place}>
              {station ? (
                <>
                  м. {station}
                  {v.metro.length > 1 && ` +${v.metro.length - 1}`}
                </>
              ) : (
                v.address
              )}
            </div>
          )}
        </div>
      </Td>
      {/* «1–3 года» wraps only where 1280 px needs the room */}
      <Td className="text-caption text-ink-2 2xl:whitespace-nowrap">{experienceShort(v.experience_name) ?? '—'}</Td>
      <Td className="whitespace-nowrap text-caption text-ink-2" title={formatDateTime(v.published_at)}>
        {shortAgo(v.published_at)}
      </Td>
      <Td align="right" className="text-caption text-ink-2">
        {v.responses_count ?? '—'}
      </Td>
      <Td>
        <div>
          <div className="text-caption text-ink-2">{v.status_label}</div>
          {v.snoozed_until && (
            <div className="mt-0.5 inline-flex items-center gap-1 text-xs text-ink-3">
              <Clock aria-hidden className="size-3" />
              до {shortDate(v.snoozed_until)}
            </div>
          )}
          {v.score !== null && (
            <div className="mt-1">
              <Badge tone={letter.tone}>{letter.label}</Badge>
            </div>
          )}
        </div>
      </Td>
    </tr>
  )
}

function SkeletonRow() {
  return (
    <tr aria-hidden>
      <Td />
      <Td>
        <Skeleton className="h-6 w-9" />
      </Td>
      <Td>
        <Skeleton className="h-4 w-56" />
        <Skeleton className="mt-1.5 h-3 w-32" />
      </Td>
      <Td>
        <Skeleton className="h-4 w-28" />
      </Td>
      <Td>
        <Skeleton className="ml-auto h-4 w-20" />
      </Td>
      <Td>
        <Skeleton className="h-4 w-24" />
      </Td>
      <Td>
        <Skeleton className="h-4 w-14" />
      </Td>
      <Td>
        <Skeleton className="h-4 w-16" />
      </Td>
      <Td>
        <Skeleton className="ml-auto h-4 w-8" />
      </Td>
      <Td>
        <Skeleton className="h-5 w-24" />
      </Td>
    </tr>
  )
}

/** The hidden employers under the table, as before the redesign: the cross shows one again. */
export function HiddenEmployersPanel({ items, onUnhide, busy }: { items: HiddenEmployer[]; onUnhide: (id: number) => void; busy: boolean }) {
  return (
    <Panel title="Скрытые работодатели" meta="Их вакансии не попадают в кандидаты. Крестик снова показывает работодателя и возвращает вакансии, скрытые вместе с ним.">
      <div className="flex flex-wrap gap-1.5">
        {items.map((h) => {
          const name = h.employer_name || h.employer_id || `№${h.id}`
          return (
            <Chip key={h.id} onRemove={() => !busy && onUnhide(h.id)} removeLabel={`Показывать снова: ${name}`}>
              {name}
            </Chip>
          )
        })}
      </div>
    </Panel>
  )
}
