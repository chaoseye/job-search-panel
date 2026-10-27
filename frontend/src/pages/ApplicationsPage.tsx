import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDownWideNarrow, ArrowUpNarrowWide, ChevronDown, MoreHorizontal, RefreshCw, Send } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiPost } from '../api/client'
import type { ApplicationList, ApplicationRow, ApplyStatus, SyncStatus } from '../api/types'
import AssistPanel from '../components/AssistPanel'
import NegotiationDrawer, { STATE_TONE } from '../components/NegotiationDrawer'
import { Badge, Count, type BadgeTone } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { cx } from '../components/ui/cx'
import { Input, Select } from '../components/ui/Field'
import { Menu, type MenuEntry } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Pagination } from '../components/ui/Pagination'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { SortTh, Table, Td, Th } from '../components/ui/Table'
import { Tabs } from '../components/ui/Tabs'
import { countOf, formatDateTime, relativeTime, shortAgo } from '../lib/format'

const PER_PAGE = 50

const STATUS_TONE: Record<string, BadgeTone> = {
  queued: 'info',
  sending: 'info',
  sent: 'good',
  failed: 'bad',
  needs_manual: 'warn',
  cancelled: 'neutral',
}

/** Local statuses of the panel (M4) and hh.ru stages from sync (M5, F-8.9.4). */
const FILTERS: { id: string; label: string; status?: string; stage?: string }[] = [
  { id: 'all', label: 'Все' },
  { id: 'queued', label: 'В очереди', status: 'queued,sending' },
  { id: 'needs_manual', label: 'Нужно действие', status: 'needs_manual' },
  { id: 'failed', label: 'Не удались', status: 'failed' },
  { id: 'cancelled', label: 'Отменены', status: 'cancelled' },
  { id: 'sent', label: 'Отправлены', stage: 'sent' },
  { id: 'viewed', label: 'Просмотрены', stage: 'viewed' },
  { id: 'invitation', label: 'Приглашения', stage: 'invitation' },
  { id: 'discard', label: 'Отказы', stage: 'discard' },
  { id: 'unread', label: 'Есть обновления', stage: 'unread' },
  { id: 'no_answer', label: 'Без ответа', stage: 'no_answer' },
  { id: 'hidden', label: 'Скрытые', stage: 'hidden' },
]
const LABEL = Object.fromEntries(FILTERS.map((f) => [f.id, f.label])) as Record<string, string>
/** The views worth a tab (the ones that wait for the user are accented); the rest under «Ещё». */
const PRIMARY = ['all', 'unread', 'invitation', 'no_answer', 'needs_manual']
const ATTENTION = ['unread', 'needs_manual']
const MORE: { section: string; ids: string[] }[] = [
  { section: 'На hh.ru', ids: ['sent', 'viewed', 'discard', 'hidden'] },
  { section: 'Очередь панели', ids: ['queued', 'failed', 'cancelled'] },
]
const SORTS: [string, string][] = [
  ['sent', 'Дата отправки'],
  ['viewed', 'Просмотр'],
  ['updated', 'Обновление на hh.ru'],
  ['status', 'Статус'],
  ['vacancy', 'Вакансия'],
  ['employer', 'Компания'],
  ['mode', 'Способ'],
]

export default function ApplicationsPage() {
  const qc = useQueryClient()
  const [filter, setFilter] = useState('all')
  const [mode, setMode] = useState('')
  const [days, setDays] = useState(7)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [page, setPage] = useState(0)
  const [sort, setSort] = useState('sent')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [assist, setAssist] = useState<ApplicationRow | null>(null)
  const [open, setOpen] = useState<ApplicationRow | null>(null)
  const f = FILTERS.find((x) => x.id === filter) ?? FILTERS[0]
  const qs = new URLSearchParams({ page: String(page), per_page: String(PER_PAGE), days: String(days), sort, order })
  if (f.status) qs.set('status', f.status)
  if (f.stage) qs.set('stage', f.stage)
  if (mode) qs.set('mode', mode)
  if (dateFrom) qs.set('date_from', dateFrom)
  if (dateTo) qs.set('date_to', dateTo)

  const toggleSort = (id: string) => {
    // first click: the natural direction of the column (dates newest first, names A→Z, statuses best first)
    if (sort === id) setOrder(order === 'asc' ? 'desc' : 'asc')
    else {
      setSort(id)
      setOrder(id === 'sent' || id === 'viewed' || id === 'updated' || id === 'score' ? 'desc' : 'asc')
    }
    setPage(0)
  }
  const status = useQuery({ queryKey: ['apply-status'], queryFn: () => apiGet<ApplyStatus>('/api/applications/status'), refetchInterval: 15000 })
  const sync = useQuery({ queryKey: ['sync-status'], queryFn: () => apiGet<SyncStatus>('/api/negotiations/status'), refetchInterval: 15000 })
  const list = useQuery({
    queryKey: ['applications', qs.toString()],
    queryFn: () => apiGet<ApplicationList>(`/api/applications?${qs.toString()}`),
    refetchInterval: 10000,
  })
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['applications'] })
    void qc.invalidateQueries({ queryKey: ['apply-status'] })
    void qc.invalidateQueries({ queryKey: ['sync-status'] })
    void qc.invalidateQueries({ queryKey: ['vacancies'] })
    void qc.invalidateQueries({ queryKey: ['vacancy-summary'] })
  }
  const act = useMutation({
    mutationFn: ({ id, action }: { id: number; action: string }) => apiPost<ApplicationRow>(`/api/applications/${id}/${action}`),
    onSuccess: invalidate,
  })
  const runNow = useMutation({ mutationFn: () => apiPost('/api/applications/run'), onSuccess: invalidate })
  const syncNow = useMutation({ mutationFn: () => apiPost('/api/negotiations/sync'), onSuccess: () => setTimeout(invalidate, 1500) })

  const s = status.data
  const items = list.data?.items ?? []
  const counts = list.data?.counts ?? {}
  const stageCounts = list.data?.stage_counts ?? {}
  const countFor = (id: string): number | undefined => {
    const x = FILTERS.find((y) => y.id === id)
    if (!x || !list.data) return undefined
    if (x.status) return x.status.split(',').reduce((a, k) => a + (counts[k] ?? 0), 0)
    if (x.stage) return stageCounts[x.stage]
    return undefined
  }
  const choose = (id: string) => {
    setFilter(id)
    setPage(0)
  }
  const narrowed = filter !== 'all' || Boolean(mode || dateFrom || dateTo)
  const syncing = sync.data?.job_running || syncNow.isPending
  const inMore = !PRIMARY.includes(filter)

  const rowActions = (a: ApplicationRow): MenuEntry[] => {
    const n = a.negotiation
    const out: MenuEntry[] = []
    if (a.letter_text || n || a.status === 'sent') out.push({ key: 'open', label: n ? 'Подробнее' : a.status === 'sent' ? 'Ответ работодателя' : 'Письмо', onSelect: () => setOpen(a) })
    if (a.negotiation_url) out.push({ key: 'neg', label: 'Отклик на hh.ru', href: a.negotiation_url })
    if (a.response_url) out.push({ key: 'ext', label: 'Сайт работодателя', href: a.response_url })
    if (a.vacancy?.alternate_url) out.push({ key: 'vac', label: 'Вакансия на hh.ru', href: a.vacancy.alternate_url })
    const danger: MenuEntry[] = []
    if (a.status === 'queued' || a.status === 'needs_manual' || a.status === 'failed') danger.push({ key: 'cancel', label: 'Отменить', tone: 'danger', onSelect: () => act.mutate({ id: a.id, action: 'cancel' }) })
    if (a.status === 'sent' && a.hh_negotiation_id && !a.dry_run && a.mode === 'api')
      danger.push({
        key: 'withdraw',
        label: 'Отозвать отклик',
        tone: 'danger',
        onSelect: () => {
          if (window.confirm('Отозвать отклик на hh.ru?')) act.mutate({ id: a.id, action: 'withdraw' })
        },
      })
    if (danger.length && out.length) out.push('separator')
    return [...out, ...danger]
  }
  const mainAction = (a: ApplicationRow) => {
    if (a.mode === 'assist' && a.status === 'needs_manual')
      return (
        <Button size="sm" variant="primary" onClick={() => setAssist({ ...a, apply_url: a.vacancy?.apply_alternate_url ?? a.vacancy?.alternate_url })}>
          Открыть и подтвердить
        </Button>
      )
    if (a.status === 'failed')
      return (
        <Button size="sm" onClick={() => act.mutate({ id: a.id, action: 'retry' })} disabled={act.isPending}>
          Повторить
        </Button>
      )
    return null
  }
  const clickable = (a: ApplicationRow) => Boolean(a.negotiation || a.status === 'sent')

  const metaLine1: string[] = []
  if (s) {
    metaLine1.push(s.apply_mode === 'api' ? 'Отклики через API' : 'Режим «Ассистент»')
    if (s.dry_run) metaLine1.push('сухой прогон')
    if (s.autopilot) metaLine1.push(`автопилот от оценки ${s.autopilot_min_score}`)
    metaLine1.push(`сегодня ${s.limits.today_sent} из ${s.limits.max_per_day}, за час ${s.limits.hour_sent} из ${s.limits.max_per_hour}, окно ${s.limits.window}`)
  }
  const metaLine2 = sync.data
    ? `${sync.data.last_sync_at ? `Статусы и переписка обновлены ${relativeTime(sync.data.last_sync_at)}` : 'Статусы с hh.ru ещё не обновлялись'} · каждые ${sync.data.interval_min} мин`
    : ''

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Отклики"
        meta={
          <>
            {metaLine1.join(' · ')}
            {metaLine2 && <div>{metaLine2}</div>}
          </>
        }
        actions={
          s && (
            <>
              {s.apply_mode === 'api' && (
                <Button onClick={() => runNow.mutate()} disabled={runNow.isPending || s.job_running || !(counts.queued > 0)}>
                  <Send aria-hidden />
                  {s.job_running ? 'Отправка идёт…' : 'Отправить очередь'}
                </Button>
              )}
              <Button
                onClick={() => syncNow.mutate()}
                disabled={syncing || sync.data?.capability === 'unavailable'}
                title={sync.data?.last_sync_at ? `Последняя синхронизация ${formatDateTime(sync.data.last_sync_at)}` : 'Ещё не выполнялась'}
              >
                <RefreshCw aria-hidden className={cx(syncing && 'animate-spin motion-reduce:animate-none')} />
                {sync.data?.job_running ? 'Синхронизация…' : 'Синхронизировать'}
              </Button>
            </>
          )
        }
      />

      {sync.data?.capability === 'unavailable' && (
        <Notice tone="warn" title="Синхронизация с hh.ru отключена">
          Приложению недоступен список откликов (GET /negotiations, по зонду). Что ответил работодатель, можно записать вручную в карточке отклика.
        </Notice>
      )}
      {s && s.limits.blocked_reasons.length > 0 && s.apply_mode === 'api' && (
        <Notice tone="warn" title="Отправка сейчас невозможна">
          {s.limits.blocked_reasons.join('; ')}
        </Notice>
      )}
      {s && s.limits.blocked_reasons.length > 0 && s.apply_mode === 'assist' && (
        <Notice>
          Лимиты автоотправки через API ({s.limits.blocked_reasons.join('; ')}) в режиме «Ассистент» не мешают: отклики вы отправляете сами на hh.ru. Панель считает их только для
          статистики, а частоту откликов hh.ru контролирует со своей стороны.
        </Notice>
      )}
      {(act.error || runNow.error || syncNow.error) && <ErrorNotice error={act.error ?? runNow.error ?? syncNow.error} title="Действие не выполнено" />}

      {assist && (
        <AssistPanel
          application={assist}
          onDone={() => {
            setAssist(null)
            invalidate()
          }}
        />
      )}

      <section aria-label="Список откликов">
        <Tabs
          label="Группы откликов"
          value={inMore ? '' : filter}
          onChange={choose}
          items={PRIMARY.map((id) => ({ id, label: LABEL[id], count: id === 'all' ? undefined : countFor(id), attention: ATTENTION.includes(id) }))}
          trailing={
            <Menu
              label="Другие группы"
              items={MORE.flatMap(({ section, ids }) => [
                { section },
                ...ids.map((id) => ({
                  key: id,
                  label: (
                    <span className="flex items-center justify-between gap-6">
                      <span>{LABEL[id]}</span>
                      <span className="text-xs tabular-nums text-ink-3">{countFor(id) ?? ''}</span>
                    </span>
                  ),
                  checked: filter === id,
                  onSelect: () => choose(id),
                })),
              ])}
              trigger={(p) => (
                <button
                  {...p}
                  type="button"
                  className={cx(
                    'flex h-10 items-center gap-1 whitespace-nowrap border-b-2 pl-3 text-sm transition-colors',
                    'focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus',
                    inMore ? 'border-ink font-medium text-ink' : 'border-transparent text-ink-2 hover:text-ink',
                  )}
                >
                  {inMore ? LABEL[filter] : 'Ещё'}
                  {inMore && countFor(filter) !== undefined && <Count value={countFor(filter)!} />}
                  <ChevronDown aria-hidden className="size-4 text-ink-3" />
                </button>
              )}
            />
          }
        />

        <div className="flex flex-wrap items-center gap-2 py-3">
          <Select aria-label="Способ отклика" wrapperClassName="w-auto" className="min-w-44" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="">Любой способ</option>
            <option value="api">Через API</option>
            <option value="assist">Вручную на hh.ru</option>
            <option value="external">Вне панели</option>
          </Select>
          <span className="flex items-center gap-1.5">
            <Input type="date" aria-label="Отправлен с" title="Отправлен с" className="w-[9.5rem]" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
            <span className="text-ink-3">–</span>
            <Input type="date" aria-label="Отправлен по" title="Отправлен по" className="w-[9.5rem]" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
          </span>
          {filter === 'no_answer' && (
            <label className="flex items-center gap-1.5 text-caption text-ink-2">
              дольше
              <Input type="number" min={1} max={365} className="w-16" value={days} onChange={(e) => setDays(Math.max(1, Number(e.target.value) || 7))} />
              дн.
            </label>
          )}
          <Menu
            label="Сортировка"
            align="start"
            items={SORTS.map(([id, label]) => ({ key: id, label, checked: sort === id, onSelect: () => toggleSort(id) }))}
            trigger={(p) => (
              <Button {...p} variant="ghost" title="Повторный выбор той же сортировки меняет порядок">
                {order === 'asc' ? <ArrowUpNarrowWide aria-hidden /> : <ArrowDownWideNarrow aria-hidden />}
                {SORTS.find(([id]) => id === sort)?.[1]}
                <span className="sr-only">{order === 'asc' ? ', по возрастанию' : ', по убыванию'}</span>
              </Button>
            )}
          />
          {list.data && <span className="ml-auto text-caption tabular-nums text-ink-3">{countOf(list.data.total, ['отклик', 'отклика', 'откликов'])}</span>}
        </div>

        <div className="overflow-clip rounded-lg border border-line bg-surface">
          {list.isLoading && (
            <div aria-busy="true" aria-label="Загрузка откликов" className="divide-y divide-rule">
              {Array.from({ length: 6 }, (_, i) => (
                <div key={i} className="space-y-2 px-5 py-3">
                  <Skeleton className="h-4 w-2/5" />
                  <Skeleton className="h-3 w-1/4" />
                </div>
              ))}
            </div>
          )}
          {list.error && <ErrorNotice className="m-4" error={list.error} title="Не удалось загрузить отклики" onRetry={() => void list.refetch()} />}
          {list.data && items.length === 0 && (
            <EmptyState
              title={narrowed ? 'По этому фильтру откликов нет' : 'Откликов пока нет'}
              action={
                narrowed ? (
                  <Button
                    onClick={() => {
                      choose('all')
                      setMode('')
                      setDateFrom('')
                      setDateTo('')
                    }}
                  >
                    Показать все отклики
                  </Button>
                ) : (
                  <Link className="text-sm font-medium text-accent underline-offset-2 hover:underline" to="/vacancies">
                    Открыть вакансии
                  </Link>
                )
              }
            >
              {narrowed ? undefined : 'Выберите вакансии на странице «Вакансии» и нажмите «Откликнуться».'}
            </EmptyState>
          )}

          {items.length > 0 && (
            <>
              <div className="hidden xl:block">
                <Table label="Отклики" minWidth={900}>
                  <thead>
                    <tr>
                      <SortTh id="vacancy" label="Вакансия" sort={sort} order={order} onSort={toggleSort} />
                      <SortTh id="employer" label="Компания" sort={sort} order={order} onSort={toggleSort} />
                      <SortTh id="sent" label="Отправлен" sort={sort} order={order} onSort={toggleSort} />
                      <SortTh id="status" label="Статус" sort={sort} order={order} onSort={toggleSort} />
                      <SortTh id="mode" label="Способ" sort={sort} order={order} onSort={toggleSort} />
                      <Th>
                        <span className="sr-only">Действия</span>
                      </Th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((a) => {
                      const go = clickable(a)
                      return (
                        <tr key={a.id} className={cx('transition-colors', go && 'cursor-pointer hover:bg-sunken')} onClick={() => go && setOpen(a)}>
                          <Td className="min-w-[11rem]">
                            {go ? (
                              <button
                                type="button"
                                className="rounded-sm text-left font-medium text-ink"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  setOpen(a)
                                }}
                              >
                                {a.vacancy?.name ?? `Вакансия №${a.vacancy_id}`}
                              </button>
                            ) : (
                              <span className="font-medium text-ink">{a.vacancy?.name ?? `Вакансия №${a.vacancy_id}`}</span>
                            )}
                            <RowNotes a={a} />
                          </Td>
                          <Td className="text-ink-2">{a.vacancy?.employer_name ?? '—'}</Td>
                          <Td className="whitespace-nowrap tabular-nums text-ink-2" title={formatDateTime(a.sent_at ?? a.queued_at)}>
                            {shortAgo(a.sent_at ?? a.queued_at)}
                          </Td>
                          <Td>
                            <StatusCell a={a} />
                          </Td>
                          <Td className="whitespace-nowrap text-ink-2">{a.mode_label}</Td>
                          <Td className="w-px whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                            <div className="flex items-center justify-end gap-1">
                              {mainAction(a)}
                              <RowMenu items={rowActions(a)} name={a.vacancy?.name} />
                            </div>
                          </Td>
                        </tr>
                      )
                    })}
                  </tbody>
                </Table>
              </div>

              <ul className="divide-y divide-rule xl:hidden" aria-label="Отклики">
                {items.map((a) => (
                  <li key={a.id} className="px-4 py-3">
                    <div className="flex items-start gap-2">
                      <div className="min-w-0 flex-1">
                        {clickable(a) ? (
                          <button type="button" className="rounded-sm text-left text-sm font-medium text-ink" onClick={() => setOpen(a)}>
                            {a.vacancy?.name ?? `Вакансия №${a.vacancy_id}`}
                          </button>
                        ) : (
                          <span className="text-sm font-medium text-ink">{a.vacancy?.name ?? `Вакансия №${a.vacancy_id}`}</span>
                        )}
                        <div className="mt-0.5 text-caption text-ink-2">
                          {a.vacancy?.employer_name ?? 'Работодатель не указан'} · {a.mode_label} · {shortAgo(a.sent_at ?? a.queued_at)}
                        </div>
                      </div>
                      <RowMenu items={rowActions(a)} name={a.vacancy?.name} />
                    </div>
                    <div className="mt-1.5">
                      <StatusCell a={a} />
                    </div>
                    <RowNotes a={a} />
                    {mainAction(a) && <div className="mt-2">{mainAction(a)}</div>}
                  </li>
                ))}
              </ul>
            </>
          )}
          {list.data && <Pagination page={page} perPage={PER_PAGE} total={list.data.total} onPage={setPage} />}
        </div>
      </section>
      {open && <NegotiationDrawer application={open} onClose={() => setOpen(null)} />}
    </div>
  )
}

function StatusCell({ a }: { a: ApplicationRow }) {
  const n = a.negotiation
  const notes: string[] = []
  if (n?.has_updates) notes.push('есть обновления')
  if (n && n.unread_count > 0) notes.push(`непрочитанных: ${n.unread_count}`)
  if (a.dry_run) notes.push('сухой прогон')
  return (
    <div className="flex flex-col items-start gap-0.5">
      {a.status === 'sent' && n ? <Badge tone={STATE_TONE[n.state_id ?? 'response'] ?? 'neutral'}>{n.state_label}</Badge> : <Badge tone={STATUS_TONE[a.status] ?? 'neutral'}>{a.status_label}</Badge>}
      {notes.length > 0 && <span className="text-xs text-warn">{notes.join(' · ')}</span>}
      {n?.first_viewed_at && (
        <span className="text-xs tabular-nums text-ink-3" title={formatDateTime(n.first_viewed_at)}>
          просмотрен {relativeTime(n.first_viewed_at)}
        </span>
      )}
      {n?.hh_updated_at && (
        <span className="text-xs tabular-nums text-ink-3" title={formatDateTime(n.hh_updated_at)}>
          обновлён на hh.ru {relativeTime(n.hh_updated_at)}
        </span>
      )}
      {a.autopilot && <span className="text-xs text-ink-3">автопилот</span>}
    </div>
  )
}

function RowNotes({ a }: { a: ApplicationRow }) {
  const bits: string[] = []
  if (a.vacancy?.score !== null && a.vacancy?.score !== undefined) bits.push(`оценка ${a.vacancy.score}`)
  if (a.attempts > 1) bits.push(`попыток: ${a.attempts}`)
  return (
    <>
      {bits.length > 0 && <div className="mt-0.5 text-xs text-ink-3">{bits.join(' · ')}</div>}
      {a.error_text && <div className="mt-1 text-caption text-bad">{a.error_text}</div>}
    </>
  )
}

function RowMenu({ items, name }: { items: MenuEntry[]; name?: string }) {
  if (!items.length) return null
  return (
    <Menu
      label="Действия с откликом"
      items={items}
      trigger={(p) => (
        <Button {...p} variant="ghost" size="sm" iconOnly aria-label={`Действия с откликом${name ? `: ${name}` : ''}`} title="Действия">
          <MoreHorizontal aria-hidden />
        </Button>
      )}
    />
  )
}
