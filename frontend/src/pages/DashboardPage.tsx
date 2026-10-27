import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Download, OctagonAlert } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet } from '../api/client'
import type { BreakdownRow, DeliverySummary, NotificationRow, StatsView, SyncStatus, VacancyList } from '../api/types'
import { DailyChart, HBars, Heatmap, OUTCOMES } from '../components/charts'
import { Badge } from '../components/ui/Badge'
import { Button, buttonClass } from '../components/ui/Button'
import { Field, Input } from '../components/ui/Field'
import { Menu } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Panel } from '../components/ui/Panel'
import { Segmented } from '../components/ui/Segmented'
import { Stat } from '../components/ui/Stat'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { Tabs } from '../components/ui/Tabs'
import { formatDateTime, relativeTime, shortAgo } from '../lib/format'

const PERIODS: [string, string][] = [
  ['today', 'Сегодня'],
  ['7d', '7 дней'],
  ['30d', '30 дней'],
  ['all', 'Всё время'],
  ['custom', 'Период'],
]

/** Definitions shown as tooltips (F-8.10.5), mirroring the docstring of services/stats.py. */
const DEFS: Record<string, string> = {
  found: 'Вакансии, впервые найденные сканером за период',
  passed_rules: 'Из найденных: прошли жёсткие правила отбора',
  evaluated: 'Вакансии с оценкой Claude, созданной за период',
  suitable: 'Оценки за период с вердиктом «подходит»',
  sent: 'Отклики в статусе «отправлен» (не сухой прогон) с датой отправки в периоде: API / вручную / вне панели',
  viewed: 'Отправленные за период, которые работодатель просмотрел',
  invitations: 'Отправленные за период, дошедшие до статуса «приглашение»',
  discards: 'Отправленные за период со статусом «отказ»',
  no_answer: 'Отправлены более N дней назад без реакции работодателя (нет приглашения, отказа и сообщений; просмотр не считается ответом)',
  conv: 'Конверсии: просмотрено/отправлено, приглашений/отправлено, приглашений/просмотрено',
  median: 'Медиана времени от отправки до первой реакции работодателя (просмотр, смена статуса или сообщение), часы',
  remaining: 'Сколько откликов ещё разрешает дневной лимит панели сегодня',
  batches: 'Пакетов для Claude создано / импортировано; писем Claude получено за период',
}

const BREAKDOWNS: [keyof StatsView['breakdowns'], string][] = [
  ['by_search', 'По запросам'],
  ['by_employer', 'По работодателям'],
  ['by_salary', 'По зарплате'],
  ['by_work_format', 'По формату'],
  ['by_area', 'По региону'],
  ['by_weekday_hour', 'По дню и часу'],
]

function pct(v: number | null): string {
  return v === null || v === undefined ? '—' : `${v.toFixed(1)}%`
}

/** «2026-10-15» → «15.10» (the year only when it is not this one); a date of the period, no time zone involved. */
function day(iso: string): string {
  const [y, m, d] = iso.split('-')
  return y === String(new Date().getFullYear()) ? `${d}.${m}` : `${d}.${m}.${y.slice(2)}`
}

/** Vacancies whose current letter still has [[УТОЧНИТЬ]] or open questions and can still be sent (the group
 *  needs_clarification): the panel will not send them until the letter is finished. */
export function ClarifyBlock() {
  const q = useQuery({
    queryKey: ['vacancies', 'clarify-dashboard'],
    queryFn: () => apiGet<VacancyList>('/api/vacancies?group=needs_clarification&sort=score&per_page=5'),
    refetchInterval: 60000,
  })
  const total = q.data?.total ?? 0
  if (!q.data || total === 0) return null
  return (
    <section className="rounded-lg border border-warn/25 bg-warn-soft px-4 py-3.5" aria-label="Письма ждут уточнений">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-ink">
          <AlertTriangle aria-hidden className="size-4 shrink-0 text-warn" />
          Письма ждут уточнений: {total}
        </h2>
        <Link className={buttonClass('secondary', 'sm')} to="/vacancies?group=needs_clarification">
          Показать все
        </Link>
      </div>
      <p className="mt-1 text-caption text-ink-2">Claude оставил в письме пометку [[УТОЧНИТЬ]] или вопрос к вам. Такой отклик панель не отправит, пока письмо не дописано.</p>
      <ul className="mt-2 space-y-1 text-sm">
        {q.data.items.map((v) => (
          <li key={v.id} className="min-w-0 truncate">
            <Link className="font-medium text-ink underline-offset-2 hover:underline" to={`/vacancies?open=${v.id}`}>
              {v.name}
            </Link>
            <span className="text-ink-2">
              {v.employer.name ? ` · ${v.employer.name}` : ''} · оценка {v.score ?? '—'}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** Telegram messages the panel gave up on during the last week (a wrong chat_id, a blocked bot, a day without
 *  network): before, the only trace was a counter in «Настройки → Telegram». */
export function DeliveryBlock() {
  const q = useQuery({
    queryKey: ['notifications', 'summary'],
    queryFn: () => apiGet<DeliverySummary>('/api/notifications/summary'),
    refetchInterval: 60000,
  })
  if (!q.data || q.data.failed === 0) return null
  const last = q.data.last_failed
  return (
    <section className="rounded-lg border border-bad/20 bg-bad-soft px-4 py-3.5" aria-label="Не доставлено в Telegram">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-ink">
          <OctagonAlert aria-hidden className="size-4 shrink-0 text-bad" />
          Не доставлено в Telegram за {q.data.days} дней: {q.data.failed}
        </h2>
        <Link className={buttonClass('secondary', 'sm')} to="/settings#telegram">
          Настройки Telegram
        </Link>
      </div>
      {last && (
        <p className="mt-1 text-caption text-ink-2">
          Последнее: {last.label}
          {last.created_at ? `, ${formatDateTime(last.created_at)}` : ''} — {last.error || 'без текста ошибки'}
        </p>
      )}
    </section>
  )
}

/** «Сводка»: what waits for the user first, then the numbers of the period, the charts and the event feed. */
export default function DashboardPage() {
  const [period, setPeriod] = useState('7d')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [breakdown, setBreakdown] = useState<keyof StatsView['breakdowns']>('by_search')
  const ready = period !== 'custom' || Boolean(from)
  const qs = new URLSearchParams({ period })
  if (period === 'custom') {
    qs.set('date_from', from)
    if (to) qs.set('date_to', to)
  }
  const stats = useQuery({
    queryKey: ['stats', period, from, to],
    queryFn: () => apiGet<StatsView>(`/api/stats?${qs.toString()}`),
    enabled: ready,
    refetchInterval: 60000,
  })
  const sync = useQuery({ queryKey: ['sync-status'], queryFn: () => apiGet<SyncStatus>('/api/negotiations/status'), refetchInterval: 30000 })
  const feed = useQuery({ queryKey: ['notifications-ui'], queryFn: () => apiGet<NotificationRow[]>('/api/notifications?channel=ui&limit=30'), refetchInterval: 30000 })
  const s = stats.data
  const m = s?.metrics

  const meta: string[] = []
  if (s) meta.push(`Период ${day(s.period.date_from)}–${day(s.period.date_to)}`)
  if (sync.data) meta.push(sync.data.last_sync_at ? `отклики сверены с hh.ru ${relativeTime(sync.data.last_sync_at)}` : 'отклики с hh.ru ещё не сверялись')

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Сводка"
        meta={meta.join(' · ')}
        actions={
          <>
            <Segmented label="Период" options={PERIODS} value={period} onChange={setPeriod} />
            {ready && (
              <Menu
                label="Выгрузить в CSV"
                items={[
                  { key: 'applications', label: 'Отклики за период', hint: 'по строке на отклик', href: `/api/stats/export?kind=applications&${qs.toString()}`, download: true },
                  { key: 'aggregates', label: 'Цифры за период', hint: 'показатели и разрезы', href: `/api/stats/export?kind=aggregates&${qs.toString()}`, download: true },
                ]}
                trigger={(p) => (
                  <Button {...p} variant="ghost">
                    <Download aria-hidden />
                    CSV
                  </Button>
                )}
              />
            )}
          </>
        }
      />

      {period === 'custom' && (
        <div className="-mt-2 flex flex-wrap items-end gap-3">
          <Field label="С" className="w-40">
            <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </Field>
          <Field label="По" className="w-40">
            <Input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </Field>
          {!from && <p className="pb-2 text-caption text-ink-3">Выберите начало периода.</p>}
        </div>
      )}

      {sync.data?.capability === 'unavailable' && (
        <Notice tone="warn" title="Статусы откликов не обновляются">
          hh.ru не даёт приложению список откликов (GET /negotiations), поэтому просмотры, приглашения и отказы записываются вручную в карточке отклика.
        </Notice>
      )}

      <div className="grid grid-cols-1 gap-4 empty:hidden lg:grid-cols-2 lg:[&>:only-child]:col-span-2">
        <DeliveryBlock />
        <ClarifyBlock />
      </div>

      {stats.error && <ErrorNotice error={stats.error} title="Не удалось загрузить статистику" onRetry={() => void stats.refetch()} />}
      {stats.isLoading && (
        <div aria-busy="true" aria-label="Загрузка показателей" className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-6">
          {Array.from({ length: 12 }, (_, i) => (
            <div key={i} className="rounded-lg border border-line bg-surface px-4 py-3">
              <Skeleton className="h-3.5 w-24" />
              <Skeleton className="mt-2.5 h-7 w-14" />
            </div>
          ))}
        </div>
      )}

      {m && s && (
        <>
          <section aria-label="Показатели за период" className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-6">
            <Stat label="Найдено" value={m.found} hint={DEFS.found} />
            <Stat label="Прошли правила" value={m.passed_rules} hint={DEFS.passed_rules} />
            <Stat label="Разобрано Claude" value={m.evaluated} hint={DEFS.evaluated} />
            <Stat label="Подходят" value={m.suitable} hint={DEFS.suitable} />
            <Stat label="Отправлено" value={m.sent} sub={`API ${m.sent_api} · вручную ${m.sent_assist} · вне панели ${m.sent_external}`} hint={DEFS.sent} />
            <Stat label="Просмотрено" value={m.viewed} sub={`${pct(m.conv_sent_viewed)} от отправленных`} hint={DEFS.viewed} />
            <Stat
              label="Приглашений"
              value={m.invitations}
              sub={`${pct(m.conv_sent_invited)} от отправленных · ${pct(m.conv_viewed_invited)} от просмотренных`}
              hint={`${DEFS.invitations}. ${DEFS.conv}`}
              tone="good"
            />
            <Stat label="Отказов" value={m.discards} hint={DEFS.discards} />
            <Stat label={`Без ответа больше ${s.no_answer_days} дн.`} value={m.no_answer} hint={DEFS.no_answer} />
            <Stat label="Медиана реакции" value={m.median_reaction_hours === null ? '—' : `${m.median_reaction_hours} ч`} hint={DEFS.median} />
            <Stat label="Осталось на сегодня" value={`${m.remaining_day} из ${m.max_per_day}`} hint={DEFS.remaining} />
            <Stat label="Пакеты и письма" value={`${m.batches_created} / ${m.batches_imported} / ${m.letters_imported}`} sub={`ждут разбора сейчас: ${m.awaiting_claude}`} hint={DEFS.batches} />
          </section>

          <div className="grid gap-4 xl:grid-cols-2">
            <Panel
              title="Отправлено по дням"
              meta={
                <span className="flex flex-wrap gap-x-3 gap-y-1">
                  {OUTCOMES.map((o) => (
                    <span key={o.key} className="inline-flex items-center gap-1.5">
                      <span aria-hidden className={`size-2.5 rounded-sm ${o.swatch}`} />
                      {o.label}
                    </span>
                  ))}
                </span>
              }
            >
              <DailyChart days={s.series} stacks={OUTCOMES.map((o) => o.key)} />
            </Panel>
            <Panel
              title="Найдено и подходит по дням"
              meta={
                <span className="flex flex-wrap gap-x-3 gap-y-1">
                  <span className="inline-flex items-center gap-1.5">
                    <span aria-hidden className="h-0.5 w-4 rounded bg-ink-3" />
                    найдено
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span aria-hidden className="h-0.5 w-4 rounded bg-good" />
                    подходит по оценке Claude
                  </span>
                </span>
              }
            >
              <DailyChart
                days={s.series}
                stacks={[]}
                lines={[
                  { key: 'found', className: 'stroke-ink-3', label: 'найдено' },
                  { key: 'suitable', className: 'stroke-good', label: 'подходит' },
                ]}
              />
            </Panel>
            <Panel title="Воронка">
              <HBars rows={s.funnel.map((f) => ({ label: f.label, value: f.value }))} />
            </Panel>
            <Panel title="Оценка Claude и приглашения" meta="Доля приглашений среди отправленных в каждой корзине оценки: так видно, предсказывает ли оценка результат." flush>
              <Table label="Приглашения по оценке Claude">
                <thead>
                  <tr>
                    <Th>Оценка</Th>
                    <Th align="right">Отправлено</Th>
                    <Th align="right">Приглашений</Th>
                    <Th align="right">Доля</Th>
                  </tr>
                </thead>
                <tbody>
                  {s.score_buckets.map((b) => (
                    <tr key={b.bucket}>
                      <Td>{b.bucket}</Td>
                      <Td align="right">{b.sent}</Td>
                      <Td align="right">{b.invited}</Td>
                      <Td align="right">
                        <span className="inline-flex items-center justify-end gap-2">
                          <span aria-hidden className="h-1.5 w-16 rounded-full bg-sunken">
                            <span className="block h-1.5 rounded-full bg-good" style={{ width: `${Math.min(100, b.invited_pct ?? 0)}%` }} />
                          </span>
                          <span className="w-12">{pct(b.invited_pct)}</span>
                        </span>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Panel>
          </div>

          <Panel title="Разрезы" meta="Отклики за период по запросам, работодателям, зарплате, формату, региону и времени отправки.">
            <Tabs
              label="Разрез"
              value={breakdown}
              onChange={(id) => setBreakdown(id as keyof StatsView['breakdowns'])}
              items={BREAKDOWNS.map(([id, label]) => ({ id, label }))}
              className="mb-4"
            />
            {breakdown === 'by_weekday_hour' ? (
              <div className="grid gap-6 lg:grid-cols-2">
                <div>
                  <div className="mb-2 text-caption text-ink-3">Отправлено по дню недели и часу, местное время</div>
                  <Heatmap cells={s.breakdowns.by_weekday_hour.cells} weekdays={s.breakdowns.by_weekday_hour.by_weekday.map((w) => w.label)} />
                </div>
                <div>
                  <div className="mb-2 text-caption text-ink-3">По дню недели</div>
                  <HBars rows={s.breakdowns.by_weekday_hour.by_weekday.map((w) => ({ label: w.label, value: w.sent, hint: `приглашений ${w.invited}` }))} />
                </div>
              </div>
            ) : (
              <BreakdownTable rows={s.breakdowns[breakdown] as BreakdownRow[]} />
            )}
          </Panel>
        </>
      )}

      <Panel title="Лента событий" flush>
        {feed.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={feed.error} title="Лента не загрузилась" onRetry={() => void feed.refetch()} />}
        {feed.data && feed.data.length === 0 && <EmptyState inset="panel" title="Событий пока нет" />}
        {feed.data && feed.data.length > 0 && (
          <ul className="divide-y divide-rule">
            {feed.data.map((n) => (
              <li key={n.id} className="grid gap-x-4 gap-y-1 px-4 py-2.5 text-sm sm:grid-cols-[5rem_minmax(0,1fr)] sm:px-5">
                <span className="text-caption tabular-nums text-ink-3" title={formatDateTime(n.created_at)}>
                  {shortAgo(n.created_at)}
                </span>
                <span className="min-w-0">
                  <Badge className="mr-2 align-[1px]">{n.label}</Badge>
                  <span className="text-ink">{n.summary}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  )
}

function BreakdownTable({ rows }: { rows: BreakdownRow[] }) {
  if (!rows.length) return <p className="text-caption text-ink-3">Нет отправленных откликов за период.</p>
  return (
    <div className="-mx-4 sm:-mx-5">
      <Table minWidth={560} label="Отклики по разрезу">
        <thead>
          <tr>
            <Th>Разрез</Th>
            <Th align="right">Отправлено</Th>
            <Th align="right">Просмотрено</Th>
            <Th align="right">Приглашений</Th>
            <Th align="right">Отказов</Th>
            <Th align="right">Приглашений, %</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <Td className="max-w-[18rem] truncate" title={r.label}>
                {r.label}
              </Td>
              <Td align="right">{r.sent}</Td>
              <Td align="right">{r.viewed}</Td>
              <Td align="right">{r.invited}</Td>
              <Td align="right">{r.discarded}</Td>
              <Td align="right">{pct(r.invited_pct)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  )
}
