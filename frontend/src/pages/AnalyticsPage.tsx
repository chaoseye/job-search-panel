import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ExternalLink } from 'lucide-react'
import { Fragment, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiPost, errorMessage } from '../api/client'
import type { BorderlineView, EmployerRow, FunnelView, HotView, RecommendationsView, ResumeAdviceView, SalaryView, SegmentRow, SegmentsView, SkillsView, TrendsView } from '../api/types'
import { HBars } from '../components/charts'
import { Badge, type BadgeTone } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Chip } from '../components/ui/Chip'
import { cx } from '../components/ui/cx'
import { Disclosure } from '../components/ui/Disclosure'
import { Field, Input, Select } from '../components/ui/Field'
import { PageHeader } from '../components/ui/PageHeader'
import { Panel } from '../components/ui/Panel'
import { Segmented } from '../components/ui/Segmented'
import { EmptyState, ErrorNotice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { Tabs } from '../components/ui/Tabs'
import { shortAgo } from '../lib/format'

/** Analytics of vacancy segments: what a slice of the base looks like (search, criteria set, target title, tag,
 *  format, experience), where the competition is low, which skills are demanded and missing, what the money is,
 *  who answers, which employers keep posting, how it all moves week to week. */

const KINDS: [string, string][] = [
  ['search', 'Поиски'],
  ['set', 'Наборы критериев'],
  ['title', 'Целевые должности'],
  ['domain', 'Отрасли'],
  ['tasks', 'Задачи'],
  ['stack', 'Инструменты'],
  ['format', 'Формат'],
  ['experience', 'Стаж'],
]
const FUNNEL_EXTRA: [string, string][] = [
  ['letter', 'Письмо или без'],
  ['accredited', 'Аккредитация'],
  ['test', 'Тестовое'],
  ['salary', 'Зарплатная вилка'],
  ['score', 'Оценка Claude'],
]
const SCOPES: [string, string][] = [
  ['passed', 'Прошедшие правила'],
  ['all', 'Все найденные'],
  ['evaluated', 'С оценкой Claude'],
]
const PERIODS: [string, string][] = [
  ['0', 'Вся база'],
  ['30', '30 дней'],
  ['7', '7 дней'],
]
const FIT_LABELS: Record<string, string> = {
  below: 'ниже ожиданий',
  within: 'в вилке',
  above: 'выше ожиданий',
  unknown: 'не указана',
  under: 'ниже уровня',
  match: 'по уровню',
  over: 'выше уровня',
}
const TASK_TONE: Record<string, BadgeTone> = { done: 'good', cancelled: 'bad' }

type TabId = 'market' | 'improve' | 'vacancies' | 'funnel'

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${v.toFixed(v >= 10 ? 0 : 1)}%`
}
function num(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : String(v)
}
function rub(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${Math.round(v / 1000)} тыс.`
}
function scoreClass(s: number | null): string {
  if (s === null) return 'text-ink-3'
  return s >= 70 ? 'font-medium text-good' : s >= 50 ? 'text-warn' : 'text-ink-3'
}

type Slice = { by: string; key: string; label: string } | null

export default function AnalyticsPage() {
  const [tab, setTab] = useState<TabId>('market')
  const [by, setBy] = useState('search')
  const [days, setDays] = useState(0)
  const [scope, setScope] = useState('passed')
  const [slice, setSlice] = useState<Slice>(null)
  // common query tail for the slice-aware reports: period, scope and the picked segment
  const qs = `&days=${days}&scope=${scope}` + (slice ? `&by=${encodeURIComponent(slice.by)}&key=${encodeURIComponent(slice.key)}` : '')
  const segments = useQuery({
    queryKey: ['an-segments', by, days, scope],
    queryFn: () => apiGet<SegmentsView>(`/api/analytics/segments?by=${by}&days=${days}&scope=${scope}`),
    enabled: tab === 'market',
  })

  const scopeSelect = (
    <Select
      aria-label="Какие вакансии"
      wrapperClassName="w-full sm:w-auto"
      value={scope}
      onChange={(e) => setScope(e.target.value)}
      title="Прошедшие правила: без отсеянных стоп-словами и скрытых, так продажи и SMM не забивают картину навыков и зарплат"
    >
      {SCOPES.map(([id, label]) => (
        <option key={id} value={id}>
          {label}
        </option>
      ))}
    </Select>
  )
  const periodControl = <Segmented label="Период" options={PERIODS} value={String(days)} onChange={(v) => setDays(Number(v))} />

  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Аналитика" meta="Как выглядит рынок по вашим поискам, где шансы выше, что поправить в резюме и кто отвечает на отклики." />
      <Tabs
        label="Разделы аналитики"
        value={tab}
        onChange={(t) => setTab(t as TabId)}
        items={[
          { id: 'market', label: 'Срез рынка' },
          { id: 'improve', label: 'Что улучшить' },
          { id: 'vacancies', label: 'Горячие и пограничные' },
          { id: 'funnel', label: 'Воронка ответов' },
        ]}
      />

      {tab === 'market' && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <Select aria-label="Разрез" wrapperClassName="w-full sm:w-auto" value={by} onChange={(e) => setBy(e.target.value)}>
              {KINDS.map(([id, label]) => (
                <option key={id} value={id}>
                  Разрез: {label.toLowerCase()}
                </option>
              ))}
            </Select>
            {scopeSelect}
            {periodControl}
            {slice ? (
              <Chip onRemove={() => setSlice(null)} removeLabel="Сбросить срез">
                Срез: {slice.label}
              </Chip>
            ) : (
              <span className="text-caption text-ink-3">Нажмите строку сегмента: ниже покажутся его навыки, зарплаты, работодатели и тренды.</span>
            )}
          </div>
          {segments.error && <ErrorNotice error={segments.error} title="Сегменты не загрузились" onRetry={() => void segments.refetch()} />}
          {segments.isLoading && <Skeleton className="h-64 w-full" />}
          {segments.data && <SegmentsTable data={segments.data} slice={slice} onPick={(row) => setSlice(row.key === 'all' ? null : { by, key: row.key, label: row.label })} />}
          <div className="grid gap-5 xl:grid-cols-2">
            <SkillsSection qs={qs} slice={slice} />
            <SalarySection qs={qs} slice={slice} />
          </div>
          {/* wide tables: the whole width, one under the other */}
          <EmployersSection qs={qs} slice={slice} />
          <TrendsSection qs={qs} slice={slice} />
        </>
      )}

      {tab === 'improve' && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            {scopeSelect}
            {slice && (
              <Chip onRemove={() => setSlice(null)} removeLabel="Сбросить срез">
                Срез: {slice.label}
              </Chip>
            )}
          </div>
          <RecommendationsSection
            scope={scope}
            onPick={(pickBy, key, label) => {
              setSlice({ by: pickBy, key, label })
              setTab('market')
            }}
          />
          <ResumeAdviceSection slice={slice} />
        </>
      )}

      {tab === 'vacancies' && (
        <>
          <HotSection />
          <BorderlineSection />
        </>
      )}

      {tab === 'funnel' && (
        <>
          <div className="flex flex-wrap items-center gap-2">{periodControl}</div>
          <FunnelSection days={days} />
        </>
      )}
    </div>
  )
}

// ---- segments ------------------------------------------------------------------------------------------------

const SEGMENT_COLUMNS: { label: string; title?: string }[] = [
  { label: 'Найдено', title: 'Вакансий в базе (за период)' },
  { label: 'Оценено', title: 'Разобрано Claude' },
  { label: '≥ 50', title: 'Доля оценок ≥ 50 среди разобранных' },
  { label: '≥ 70', title: 'Доля оценок ≥ 70 среди разобранных' },
  { label: '₽ медиана', title: 'Медиана зарплаты (на руки, ₽) по вакансиям с зарплатой' },
  { label: 'Без ₽', title: 'Доля вакансий без зарплаты' },
  { label: 'Удалённо' },
  { label: 'Гибрид' },
  { label: 'Тест', title: 'Тестовое задание' },
  { label: 'Внешн.', title: 'Отклик на внешнем сайте' },
  { label: 'Аккр.', title: 'Аккредитованный IT-работодатель' },
  { label: 'Откликов', title: 'Медиана числа откликов на вакансию (конкуренция)' },
  { label: '< 50 откл.', title: 'Доля вакансий, где меньше 50 откликов' },
  { label: 'Отпр.', title: 'Ваших откликов отправлено' },
  { label: 'Пригл.', title: 'Приглашений / отправлено' },
  { label: 'Реакц.', title: 'Любая реакция работодателя (приглашение, отказ, сообщение) / отправлено' },
]

function SegmentsTable({ data, slice, onPick }: { data: SegmentsView; slice: Slice; onPick: (row: SegmentRow) => void }) {
  const rows = [data.total, ...data.items]
  return (
    <Panel title={`Сегменты ${data.label}`} meta="«≥ 50» означает долю среди разобранных Claude; медианы посчитаны по вакансиям с указанной зарплатой и числом откликов." flush>
      <Table label="Сегменты" density="compact">
        <thead>
          <tr>
            <Th className="sticky left-0 z-[1] bg-surface">Сегмент</Th>
            {SEGMENT_COLUMNS.map((c) => (
              <Th key={c.label} align="right" title={c.title}>
                {c.label.replace(/ /g, '\u00a0')}
              </Th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const active = slice ? slice.key === r.key : r.key === 'all'
            const muted = (v: string) => <span className="text-ink-3">{v}</span>
            return (
              <tr key={r.key} className={cx('group/row cursor-pointer transition-colors', active ? 'bg-accent-soft' : 'hover:bg-sunken', r.key === 'all' && 'font-medium')} onClick={() => onPick(r)}>
                <Td className={cx('sticky left-0 z-[1] max-w-[14rem]', active ? 'bg-accent-soft' : 'bg-surface group-hover/row:bg-sunken')}>
                  <button type="button" className="block max-w-full truncate rounded-sm text-left" title={r.label} aria-pressed={active} onClick={(e) => (e.stopPropagation(), onPick(r))}>
                    {r.label}
                  </button>
                </Td>
                <Td align="right">{r.found}</Td>
                <Td align="right">{r.evaluated}</Td>
                <Td align="right">
                  {r.ge50} {muted(pct(r.ge50_pct))}
                </Td>
                <Td align="right">
                  {r.ge70} {muted(pct(r.ge70_pct))}
                </Td>
                <Td align="right" title={`p25 ${rub(r.salary_p25)} · p75 ${rub(r.salary_p75)} · n=${r.salary_n}`}>
                  {rub(r.salary_median)}
                </Td>
                <Td align="right">{pct(r.no_salary_pct)}</Td>
                <Td align="right">{pct(r.remote_pct)}</Td>
                <Td align="right">{pct(r.hybrid_pct)}</Td>
                <Td align="right">{pct(r.test_pct)}</Td>
                <Td align="right">{pct(r.direct_pct)}</Td>
                <Td align="right">{pct(r.accredited_pct)}</Td>
                <Td align="right">{num(r.responses_median)}</Td>
                <Td align="right">{pct(r.low_competition_pct)}</Td>
                <Td align="right">{r.sent}</Td>
                <Td align="right">
                  {r.invited} {muted(pct(r.invited_pct))}
                </Td>
                <Td align="right">{pct(r.reacted_pct)}</Td>
              </tr>
            )
          })}
        </tbody>
      </Table>
    </Panel>
  )
}

// ---- recommendations -----------------------------------------------------------------------------------------

function RecoBlock({ title, empty, children, className }: { title: string; empty?: string | false; children?: ReactNode; className?: string }) {
  return (
    <div className={cx('min-w-0', className)}>
      <h3 className="mb-2 text-sm font-semibold text-ink">{title}</h3>
      {empty ? <p className="text-caption text-ink-3">{empty}</p> : children}
    </div>
  )
}

function RecommendationsSection({ scope, onPick }: { scope: string; onPick: (by: string, key: string, label: string) => void }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['an-reco', scope], queryFn: () => apiGet<RecommendationsView>(`/api/analytics/recommendations?scope=${scope}`) })
  const invalidate = () => {
    void qc.invalidateQueries({ predicate: (query) => String(query.queryKey[0]).startsWith('an-') })
    void qc.invalidateQueries({ queryKey: ['searches'] })
    void qc.invalidateQueries({ queryKey: ['profile'] })
  }
  const [done, setDone] = useState<string[]>([])
  const mark = (key: string) => setDone((d) => [...d, key])
  const createSearch = useMutation({
    mutationFn: (s: { term: string; query: string }) => apiPost<{ id: number; name: string }>('/api/analytics/recommendations/search', s),
    onSuccess: (_r, s) => {
      mark(`s:${s.term}`)
      invalidate()
    },
  })
  const raise = useMutation({
    mutationFn: (names: string[]) => apiPost<{ changed: string[] }>('/api/analytics/recommendations/weights', { names }),
    onSuccess: (_r, names) => {
      names.forEach((n) => mark(`w:${n}`))
      invalidate()
    },
  })
  const stop = useMutation({
    mutationFn: (names: string[]) => apiPost<{ added: string[] }>('/api/analytics/recommendations/stop-words', { names }),
    onSuccess: (_r, names) => {
      names.forEach((n) => mark(`x:${n}`))
      invalidate()
    },
  })
  const d = q.data
  const err = createSearch.error ?? raise.error ?? stop.error
  const doneText = (text: string) => <span className="text-caption font-medium text-good">{text}</span>
  return (
    <Panel
      title="Рекомендации"
      meta={d && `По ${d.evaluated} оценкам Claude, из них хороших (≥ 50) ${d.good}${!d.enough_data ? '. Пока мало данных: цифры ориентировочные.' : ''}`}
    >
      {q.error && <ErrorNotice error={q.error} title="Рекомендации не загрузились" onRetry={() => void q.refetch()} />}
      {q.isLoading && <Skeleton className="h-40 w-full" />}
      {err && <ErrorNotice className="mb-4" error={err} title="Действие не выполнено" />}
      {d && (
        <div className="grid gap-x-8 gap-y-6 text-sm lg:grid-cols-2">
          <RecoBlock title="Где шансы выше" empty={d.focus.length === 0 && 'Нужно хотя бы 3 оценки в сегменте.'}>
            <ul className="space-y-1.5">
              {d.focus.map((f) => (
                <li key={`${f.by}:${f.key}`}>
                  <button type="button" className="rounded-sm text-left font-medium text-accent underline-offset-2 hover:underline" onClick={() => onPick(f.by, f.key, f.label)}>
                    {f.label}
                  </button>{' '}
                  <span className="text-caption text-ink-2">
                    ({f.by_label.replace('по ', '')}) — оценка ≥ 50 у {pct(f.ge50_pct)} из {f.evaluated} · откликов {num(f.responses_median)} · {rub(f.salary_median)}
                  </span>
                </li>
              ))}
            </ul>
          </RecoBlock>
          <RecoBlock title="Поиски, которые стоит завести" empty={d.searches.length === 0 && 'Слов в названиях с долей хороших оценок от 60 % (и хотя бы с 3 оценками), которых ещё нет в поисках, не найдено.'}>
            <ul className="space-y-2">
              {d.searches.map((s) => (
                <li key={s.term} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <code className="rounded bg-sunken px-1.5 py-0.5 font-mono text-xs text-ink">{s.query}</code>
                  <span className="text-caption text-ink-2">
                    хороших {s.good} из {s.evaluated} ({pct(s.rate)}) · в базе {s.in_base}
                  </span>
                  {done.includes(`s:${s.term}`) ? (
                    doneText('Создан (выключен), смотрите в «Поисках»')
                  ) : (
                    <Button size="sm" variant="ghost" disabled={createSearch.isPending} onClick={() => createSearch.mutate({ term: s.term, query: s.query })}>
                      Создать поиск по названию
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          </RecoBlock>
          <RecoBlock title="Стоп-слова в названии" empty={d.stop_words.length === 0 && 'Слов хотя бы с 4 оценками и долей хороших до 15 % нет: правила уже отсекают лишнее.'}>
            <ul className="space-y-2">
              {d.stop_words.map((s) => (
                <li key={s.term} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-medium">{s.term}</span>
                  <span className="text-caption text-ink-2">
                    хороших {s.good} из {s.evaluated}
                  </span>
                  {done.includes(`x:${s.term}`) ? (
                    doneText('Добавлено')
                  ) : (
                    <Button size="sm" variant="ghost" disabled={stop.isPending} onClick={() => stop.mutate([s.term])}>
                      В стоп-слова
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          </RecoBlock>
          <RecoBlock title="Веса навыков" empty={d.weights.length === 0 && 'Ваши навыки, которые часто просят хорошие вакансии, уже с весом 5.'}>
            <ul className="space-y-2">
              {d.weights.map((w) => (
                <li key={w.name} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-medium">{w.name}</span>
                  <span className="text-caption text-ink-2">
                    в хороших {w.in_good}, всего {w.in_all} · вес {w.weight ?? '3 (по умолчанию)'} → {w.suggested}
                  </span>
                  {done.includes(`w:${w.name}`) ? (
                    doneText('Поднят')
                  ) : (
                    <Button size="sm" variant="ghost" disabled={raise.isPending} onClick={() => raise.mutate([w.name])}>
                      Поднять до 5
                    </Button>
                  )}
                </li>
              ))}
            </ul>
            {d.weights.length > 1 && !d.weights.every((w) => done.includes(`w:${w.name}`)) && (
              <Button size="sm" className="mt-2" disabled={raise.isPending} onClick={() => raise.mutate(d.weights.map((w) => w.name))}>
                Поднять все
              </Button>
            )}
          </RecoBlock>
          <RecoBlock title="Что добавить в резюме (или изучить)" className="lg:col-span-2">
            <div className="space-y-2">
              <div className="text-caption text-ink-2">
                Просят в хороших вакансиях, но нет ни в резюме, ни в анкете:{' '}
                {d.resume.length === 0 && <span className="text-ink-3">ничего хотя бы с 2 упоминаниями</span>}
              </div>
              {d.resume.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {d.resume.map((r) => (
                    <Badge key={r.name} tone="bad" title={`в хороших ${r.in_good}, всего ${r.in_all}`}>
                      {r.name} <span className="opacity-60">{r.in_good}</span>
                    </Badge>
                  ))}
                </div>
              )}
              {d.gaps.length > 0 && (
                <>
                  <div className="pt-1 text-caption text-ink-2">Чего Claude не нашёл в профиле при оценке хороших вакансий:</div>
                  <div className="flex flex-wrap gap-1">
                    {d.gaps.map((g) => (
                      <Badge key={g.name} tone="warn">
                        {g.name} <span className="opacity-60">{g.count}</span>
                      </Badge>
                    ))}
                  </div>
                </>
              )}
              <p className="pt-1 text-caption text-ink-3">
                Если опыт есть, упомяните его в резюме и загрузите заново; если нет, это список для изучения. Веса в анкете за отсутствующие навыки не поднимайте: они описывают вас, а не спрос.
              </p>
            </div>
          </RecoBlock>
        </div>
      )}
    </Panel>
  )
}

// ---- resume advice from Claude -------------------------------------------------------------------------------

function ResumeAdviceSection({ slice }: { slice: Slice }) {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['an-advice'], queryFn: () => apiGet<ResumeAdviceView[]>('/api/analytics/resume-advice'), refetchInterval: 30000 })
  const [notes, setNotes] = useState('')
  const [open, setOpen] = useState<number | null>(null)
  const request = useMutation({
    mutationFn: () => apiPost<{ advice: ResumeAdviceView; batch_id: string | null; advices: ResumeAdviceView[] }>('/api/analytics/resume-advice', { by: slice?.by ?? null, key: slice?.key ?? null, notes: notes || null }),
    onSuccess: (data) => {
      qc.setQueryData(['an-advice'], data.advices)
      setNotes('')
      setOpen(data.advice.id)
      void qc.invalidateQueries({ queryKey: ['batches'] })
      void qc.invalidateQueries({ queryKey: ['batches-summary'] })
    },
  })
  const items = list.data ?? []
  return (
    <Panel
      title={sliceTitle('Правки резюме от Claude', slice)}
      meta="Claude получает спрос среза (что просят, чего нет в резюме, пробелы из оценок, лучшие вакансии) и профиль, а возвращает готовые абзацы и пункты. Опыт, которого нет, уходит в «уточнить», а не в текст."
    >
      <form
        className="flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault()
          request.mutate()
        }}
      >
        <Input aria-label="Что учесть" className="sm:max-w-md" placeholder="Что учесть: например, упор на backend, не на фронт" value={notes} onChange={(e) => setNotes(e.target.value)} />
        <Button type="submit" disabled={request.isPending}>
          Запросить правки
        </Button>
      </form>
      {request.data && (
        <p className="mt-2 text-caption text-good">{request.data.batch_id ? `Задача в пакете ${request.data.batch_id} — запустите /hh-batch.` : 'Задача в очереди: попадёт в следующий пакет.'}</p>
      )}
      {request.error && <ErrorNotice className="mt-3" error={request.error} title="Правки не запрошены" />}
      {list.error && <ErrorNotice className="mt-3" error={list.error} title="Список правок не загрузился" onRetry={() => void list.refetch()} />}
      {list.data && items.length === 0 && <p className="mt-4 text-caption text-ink-3">Запросов ещё не было.</p>}
      {items.length > 0 && (
        <ul className="mt-4 space-y-2 text-sm">
          {items.map((a) => (
            <li key={a.id} className="rounded-lg border border-line">
              <button
                type="button"
                aria-expanded={open === a.id}
                className="flex w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-lg px-3.5 py-2.5 text-left transition-colors hover:bg-sunken"
                onClick={() => setOpen(open === a.id ? null : a.id)}
              >
                <ChevronDown aria-hidden className={cx('size-4 shrink-0 text-ink-3 transition-transform duration-150', open !== a.id && '-rotate-90')} />
                <span className="font-medium text-ink">{a.segment_label}</span>
                <Badge tone={TASK_TONE[a.status] ?? 'neutral'}>{a.status_label}</Badge>
                <span className="text-caption text-ink-3">
                  {a.demand && `вакансий ${a.demand.found}, хороших ${a.demand.good}`}
                  {a.demand && a.demand.missing.length > 0 && ` · нет в резюме: ${a.demand.missing.slice(0, 5).map((m) => m.name).join(', ')}`}
                  {` · ${shortAgo(a.created_at)}`}
                  {a.user_notes && ` · пометка: ${a.user_notes}`}
                </span>
              </button>
              {a.error && <div className="px-3.5 pb-2.5 text-caption text-bad">{a.error}</div>}
              {open === a.id && a.result && (
                <div className="space-y-4 border-t border-line px-3.5 py-3.5">
                  {a.result.summary && <p className="max-w-[72ch] text-prose text-ink">{a.result.summary}</p>}
                  {a.result.edits.length > 0 && (
                    <div className="space-y-2">
                      {a.result.edits.map((e, i) => (
                        <div key={i} className="rounded-lg bg-sunken px-3.5 py-2.5">
                          <div className="mb-1 text-caption text-ink-3">
                            <span className="font-medium text-ink-2">{e.section || 'Правка'}</span>
                            {e.reason && ` — ${e.reason}`}
                          </div>
                          <div className="whitespace-pre-wrap text-ink">{e.proposed}</div>
                        </div>
                      ))}
                    </div>
                  )}
                  {a.result.add_skills.length > 0 && (
                    <div className="flex flex-wrap items-center gap-1">
                      <span className="mr-1 text-caption text-ink-2">Вынести в навыки:</span>
                      {a.result.add_skills.map((s) => (
                        <Badge key={s} tone="good">
                          {s}
                        </Badge>
                      ))}
                    </div>
                  )}
                  {a.result.soften.length > 0 && (
                    <div>
                      <div className="mb-1 text-caption font-medium text-ink-2">Убрать или смягчить</div>
                      <ul className="list-disc space-y-0.5 pl-5">
                        {a.result.soften.map((s, i) => (
                          <li key={i}>{s}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {a.result.questions.length > 0 && (
                    <div>
                      <div className="mb-1 text-caption font-medium text-warn">Уточнить перед правкой</div>
                      <ul className="list-disc space-y-0.5 pl-5">
                        {a.result.questions.map((s, i) => (
                          <li key={i}>{s}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <p className="text-caption text-ink-3">Правки вносите в резюме на hh.ru сами и загрузите обновлённый файл в «Профиль и критерии»: панель пересоберёт профиль для Claude.</p>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

// ---- hot and borderline vacancies ----------------------------------------------------------------------------

function VacancyLink({ id, name, employer, url }: { id: number; name: string; employer: string | null; url?: string | null }) {
  return (
    <>
      <Link className="font-medium text-ink underline-offset-2 hover:underline" to={`/vacancies?open=${id}`}>
        {name}
      </Link>
      <span className="text-ink-2"> — {employer ?? '—'}</span>
      {url && (
        <a className="ml-1.5 inline-flex items-center gap-0.5 text-caption text-ink-3 hover:text-ink" href={url} target="_blank" rel="noreferrer" title="Открыть на hh.ru">
          hh
          <ExternalLink aria-hidden className="size-3" />
        </a>
      )}
    </>
  )
}

function HotSection() {
  const [maxResponses, setMaxResponses] = useState(50)
  const [maxAge, setMaxAge] = useState(3)
  const [minScore, setMinScore] = useState(50)
  const hot = useQuery({
    queryKey: ['an-hot', maxResponses, maxAge, minScore],
    queryFn: () => apiGet<HotView>(`/api/analytics/hot?max_responses=${maxResponses}&max_age_days=${maxAge}&min_score=${minScore}`),
  })
  return (
    <Panel title="Горячие: свежие, с малой конкуренцией" meta={hot.data && `Найдено ${hot.data.total}. Без оценки Claude берутся вакансии с предоценкой выше порога.`} flush>
      <div className="flex flex-wrap items-end gap-3 px-4 pb-4 sm:px-5">
        <Field label="Откликов не больше" className="w-36">
          <Input type="number" min={0} value={maxResponses} onChange={(e) => setMaxResponses(Number(e.target.value) || 0)} />
        </Field>
        <Field label="Не старше, дней" className="w-32">
          <Input type="number" min={1} value={maxAge} onChange={(e) => setMaxAge(Math.max(1, Number(e.target.value) || 1))} />
        </Field>
        <Field label="Оценка от" className="w-28">
          <Input type="number" min={0} max={100} value={minScore} onChange={(e) => setMinScore(Number(e.target.value) || 0)} />
        </Field>
      </div>
      {hot.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={hot.error} title="Не загрузилось" onRetry={() => void hot.refetch()} />}
      {hot.data && hot.data.items.length === 0 && <EmptyState inset="panel" title="Сейчас таких нет">Ослабьте условия или дождитесь следующей проверки поисков.</EmptyState>}
      {hot.data && hot.data.items.length > 0 && (
        <Table label="Горячие вакансии" minWidth={880}>
          <thead>
            <tr>
              <Th>Вакансия</Th>
              <Th align="right">Оценка</Th>
              <Th align="right">Предоценка</Th>
              <Th align="right">Откликов</Th>
              <Th align="right">Опубликована</Th>
              <Th align="right">На руки</Th>
              <Th>Формат</Th>
              <Th>Статус</Th>
            </tr>
          </thead>
          <tbody>
            {hot.data.items.map((v) => (
              <tr key={v.id}>
                <Td className="min-w-[18rem]">
                  <VacancyLink id={v.id} name={v.name} employer={v.employer} url={v.url} />
                  {v.has_test && (
                    <Badge tone="warn" className="ml-1.5 align-[1px]">
                      тест
                    </Badge>
                  )}
                </Td>
                <Td align="right" className={scoreClass(v.score)}>
                  {num(v.score)}
                </Td>
                <Td align="right" className="text-ink-3">
                  {num(v.prescore)}
                </Td>
                <Td align="right">{v.responses}</Td>
                <Td align="right" className="whitespace-nowrap text-ink-2">
                  {shortAgo(v.published_at)}
                </Td>
                <Td align="right" className="whitespace-nowrap">
                  {rub(v.salary)}
                </Td>
                <Td className="text-ink-2">{v.formats.join(', ') || '—'}</Td>
                <Td className="text-ink-2">{v.status}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  )
}

function BorderlineSection() {
  const qc = useQueryClient()
  const [margin, setMargin] = useState(5)
  const q = useQuery({ queryKey: ['an-borderline', margin], queryFn: () => apiGet<BorderlineView>(`/api/analytics/borderline?prescore_margin=${margin}&score_margin=${margin}`) })
  const [sent, setSent] = useState<number[]>([])
  const toBatch = useMutation({
    mutationFn: (id: number) => apiPost(`/api/vacancies/${id}/to-batch`),
    onSuccess: (_r, id) => {
      setSent((s) => [...s, id])
      void qc.invalidateQueries({ queryKey: ['vacancies'] })
      void qc.invalidateQueries({ queryKey: ['batches-summary'] })
    },
  })
  const d = q.data
  return (
    <Panel
      title="Пограничные: чуть ниже порога"
      meta={
        d &&
        `Предоценка ${d.prescore_min - d.prescore_margin}–${d.prescore_min - 1} без оценки Claude или оценка ${d.score_min - d.score_margin}–${d.score_min - 1}; найдено ${d.total}. Взгляните сами: «В разбор» отправит вакансию в следующий пакет мимо порога.`
      }
      flush
    >
      <div className="flex flex-wrap items-end gap-3 px-4 pb-4 sm:px-5">
        <Field label="Запас, баллов" className="w-32">
          <Input type="number" min={1} max={30} value={margin} onChange={(e) => setMargin(Math.max(1, Number(e.target.value) || 1))} />
        </Field>
      </div>
      {q.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={q.error} title="Не загрузилось" onRetry={() => void q.refetch()} />}
      {toBatch.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={toBatch.error} title="Вакансия не отправлена в разбор" />}
      {d && d.items.length === 0 && <EmptyState inset="panel" title="Пограничных нет" />}
      {d && d.items.length > 0 && (
        <Table label="Пограничные вакансии" minWidth={880}>
          <thead>
            <tr>
              <Th>Вакансия</Th>
              <Th align="right">Балл</Th>
              <Th align="right">Откликов</Th>
              <Th align="right">На руки</Th>
              <Th>Почему ниже</Th>
              <Th>
                <span className="sr-only">Действие</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {d.items.map((v) => (
              <tr key={v.id}>
                <Td className="min-w-[16rem]">
                  <VacancyLink id={v.id} name={v.name} employer={v.employer} />
                  <div className="text-caption text-ink-3">
                    {v.formats.join(', ')}
                    {v.published_at ? ` · ${shortAgo(v.published_at)}` : ''}
                  </div>
                </Td>
                <Td align="right" className="whitespace-nowrap">
                  {v.kind === 'score' ? 'оценка ' : 'предоценка '}
                  {v.value}
                </Td>
                <Td align="right">{num(v.responses)}</Td>
                <Td align="right" className="whitespace-nowrap">
                  {rub(v.salary)}
                </Td>
                <Td className="max-w-[26rem] text-caption text-ink-2">
                  {v.summary}
                  {v.gaps.length > 0 && <div className="text-warn">не хватает: {v.gaps.join('; ')}</div>}
                </Td>
                <Td className="w-px whitespace-nowrap">
                  {sent.includes(v.id) ? (
                    <span className="text-caption font-medium text-good">В разборе</span>
                  ) : (
                    <Button size="sm" disabled={toBatch.isPending} onClick={() => toBatch.mutate(v.id)}>
                      В разбор
                    </Button>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  )
}

// ---- skills, salary, employers, trends of the slice ----------------------------------------------------------

function sliceTitle(title: string, slice: Slice): string {
  return `${title} · ${slice ? slice.label : 'все вакансии'}`
}

function SkillsSection({ qs, slice }: { qs: string; slice: Slice }) {
  const q = useQuery({ queryKey: ['an-skills', qs], queryFn: () => apiGet<SkillsView>(`/api/analytics/skills?x=1${qs}`) })
  const d = q.data
  return (
    <Panel title={sliceTitle('Навыки и пробелы', slice)} meta={d && `Навыки указаны у ${d.with_skills} из ${d.found}; оценок Claude ${d.evaluated}.`}>
      {q.error && <ErrorNotice error={q.error} title="Навыки не загрузились" onRetry={() => void q.refetch()} />}
      {q.isLoading && <Skeleton className="h-40 w-full" />}
      {d && (
        <div className="space-y-5 text-sm">
          <div>
            <h3 className="mb-2 text-caption font-medium text-ink-2">Что просят чаще всего; зелёные есть в резюме или анкете</h3>
            <div className="flex flex-wrap gap-1">
              {d.skills.map((s) => (
                <Badge key={s.name} tone={s.in_resume ? 'good' : 'bad'} title={`${s.count} вакансий · ${pct(s.share)}`}>
                  {s.name} <span className="opacity-60">{s.count}</span>
                </Badge>
              ))}
              {d.skills.length === 0 && <span className="text-caption text-ink-3">Нет данных.</span>}
            </div>
          </div>
          {d.missing.length > 0 && (
            <div>
              <h3 className="mb-2 text-caption font-medium text-ink-2">Нет в резюме, а спрашивают: изучить или упомянуть</h3>
              <HBars rows={d.missing.map((s) => ({ label: s.name, value: s.count, hint: pct(s.share) }))} tone="bad" />
            </div>
          )}
          {d.gap_skills.length > 0 && (
            <div>
              <h3 className="mb-2 text-caption font-medium text-ink-2">Чего не хватает по оценкам Claude (упоминания в пробелах)</h3>
              <HBars rows={d.gap_skills.map((s) => ({ label: s.name, value: s.count }))} tone="warn" />
            </div>
          )}
          {d.gaps.length > 0 && (
            <Disclosure summary={`Самые частые формулировки пробелов (${d.gaps.length})`}>
              <ul className="list-disc space-y-0.5 pl-5 text-ink-2">
                {d.gaps.map((g) => (
                  <li key={g.text}>
                    {g.text} <span className="text-ink-3">×{g.count}</span>
                  </li>
                ))}
              </ul>
            </Disclosure>
          )}
          {d.red_flags.length > 0 && (
            <Disclosure summary={`Что чаще всего смущает (${d.red_flags.length})`}>
              <ul className="list-disc space-y-0.5 pl-5 text-ink-2">
                {d.red_flags.map((g) => (
                  <li key={g.text}>
                    {g.text} <span className="text-ink-3">×{g.count}</span>
                  </li>
                ))}
              </ul>
            </Disclosure>
          )}
          {d.unused.length > 0 && <p className="text-caption text-ink-3">Ваши навыки, которых нет в ключевых навыках этих вакансий: {d.unused.join(', ')}</p>}
        </div>
      )}
    </Panel>
  )
}

function SalarySection({ qs, slice }: { qs: string; slice: Slice }) {
  const q = useQuery({ queryKey: ['an-salary', qs], queryFn: () => apiGet<SalaryView>(`/api/analytics/salary?x=1${qs}`) })
  const d = q.data
  const rows = d ? [d.overall, ...d.by_format, ...d.by_experience, ...d.by_score] : []
  return (
    <Panel title={sliceTitle('Зарплата', slice)} meta="На руки, ₽; вилки hh.ru приведены к «на руки» по верхней границе." flush>
      {q.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={q.error} title="Зарплаты не загрузились" onRetry={() => void q.refetch()} />}
      {q.isLoading && <Skeleton className="mx-5 mb-5 h-40" />}
      {d && (
        <>
          <Table label="Зарплаты по группам">
            <thead>
              <tr>
                <Th>Группа</Th>
                <Th align="right">С ₽ / всего</Th>
                <Th align="right">p25</Th>
                <Th align="right">Медиана</Th>
                <Th align="right">p75</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.label ?? i}>
                  <Td>{r.label ?? 'все'}</Td>
                  <Td align="right" className="text-ink-3">
                    {r.n} / {r.of}
                  </Td>
                  <Td align="right">{rub(r.p25)}</Td>
                  <Td align="right" className="font-medium">
                    {rub(r.median)}
                  </Td>
                  <Td align="right">{rub(r.p75)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <div className="space-y-3 px-4 py-4 text-sm sm:px-5">
            <p className="text-ink-2">
              {d.mine.expected ? (
                <>
                  Ваш минимум из анкеты — <span className="font-semibold text-ink">{rub(d.mine.expected)}</span>: его покрывают {d.mine.at_or_above} из {d.overall.n} вакансий с зарплатой (
                  {pct(d.mine.at_or_above_pct)}).
                </>
              ) : (
                'В анкете не указана минимальная зарплата: укажите её, и здесь появится доля вакансий, которые её покрывают.'
              )}
            </p>
            {Object.keys(d.salary_fit).length > 0 && (
              <div className="space-y-1 text-caption text-ink-2">
                <div>
                  По оценкам Claude, зарплата:{' '}
                  {Object.entries(d.salary_fit).map(([k, v]) => (
                    <span key={k} className="mr-3">
                      {FIT_LABELS[k] ?? k} <span className="font-semibold tabular-nums text-ink">{v}</span>
                    </span>
                  ))}
                </div>
                <div>
                  Уровень:{' '}
                  {Object.entries(d.seniority_fit).map(([k, v]) => (
                    <span key={k} className="mr-3">
                      {FIT_LABELS[k] ?? k} <span className="font-semibold tabular-nums text-ink">{v}</span>
                    </span>
                  ))}
                </div>
              </div>
            )}
            {d.top_paid.length > 0 && (
              <Disclosure summary="Самые высокие вилки">
                <ul className="space-y-1">
                  {d.top_paid.map((v) => (
                    <li key={v.id}>
                      <VacancyLink id={v.id} name={v.name} employer={v.employer} /> <span className="text-ink-3">·</span>{' '}
                      <span className="font-semibold tabular-nums">{rub(v.salary)}</span>
                      {v.score !== null && <span className={cx('ml-1', scoreClass(v.score))}>· оценка {v.score}</span>}
                    </li>
                  ))}
                </ul>
              </Disclosure>
            )}
          </div>
        </>
      )}
    </Panel>
  )
}

function EmployersSection({ qs, slice }: { qs: string; slice: Slice }) {
  const [open, setOpen] = useState<string | null>(null)
  const q = useQuery({ queryKey: ['an-employers', qs], queryFn: () => apiGet<{ total: number; items: EmployerRow[] }>(`/api/analytics/employers?limit=40${qs}`) })
  const d = q.data
  return (
    <Panel title={sliceTitle('Работодатели', slice)} meta={d && `${d.total} работодателей; «повторно» означает вакансии, переопубликованные после первого появления.`} flush>
      {q.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={q.error} title="Работодатели не загрузились" onRetry={() => void q.refetch()} />}
      {q.isLoading && <Skeleton className="mx-5 mb-5 h-40" />}
      {d && (
        <Table label="Работодатели" minWidth={640} density="compact">
          <thead>
            <tr>
              <Th>Работодатель</Th>
              <Th align="right">Вакансий</Th>
              <Th align="right" title="Переопубликованы после первого появления в базе">
                Повторно
              </Th>
              <Th align="right">Откликов, медиана</Th>
              <Th align="right">Лучшая оценка</Th>
              <Th align="right">Наши отклики</Th>
              <Th align="right">Пригл. / отказ</Th>
            </tr>
          </thead>
          <tbody>
            {d.items.map((e) => {
              const key = e.employer_id ?? e.name
              const expanded = open === key
              return (
                <Fragment key={key}>
                  <tr className="cursor-pointer transition-colors hover:bg-sunken" onClick={() => setOpen(expanded ? null : key)}>
                    <Td className="min-w-[14rem]">
                      <button type="button" aria-expanded={expanded} className="inline-flex items-center gap-1 rounded-sm text-left font-medium text-ink" onClick={(ev) => (ev.stopPropagation(), setOpen(expanded ? null : key))}>
                        <ChevronDown aria-hidden className={cx('size-4 shrink-0 text-ink-3 transition-transform duration-150', !expanded && '-rotate-90')} />
                        {e.name}
                      </button>
                      {e.accredited && (
                        <Badge tone="outline" className="ml-1.5 align-[1px]">
                          IT
                        </Badge>
                      )}
                      {e.hidden && (
                        <Badge className="ml-1.5 align-[1px]">скрыт</Badge>
                      )}
                      {e.url && (
                        <a className="ml-1.5 inline-flex items-center gap-0.5 text-caption text-ink-3 hover:text-ink" href={e.url} target="_blank" rel="noreferrer" onClick={(ev) => ev.stopPropagation()} title="Открыть на hh.ru">
                          hh
                          <ExternalLink aria-hidden className="size-3" />
                        </a>
                      )}
                    </Td>
                    <Td align="right">
                      {e.vacancies}
                      {e.active !== e.vacancies && <span className="text-ink-3"> ({e.active} акт.)</span>}
                    </Td>
                    <Td align="right">{e.republished || ''}</Td>
                    <Td align="right">{num(e.responses_median)}</Td>
                    <Td align="right" className={scoreClass(e.best_score)}>
                      {num(e.best_score)}
                    </Td>
                    <Td align="right">{e.sent || ''}</Td>
                    <Td align="right">
                      {e.sent ? (
                        <>
                          <span className="text-good">{e.invited}</span> / <span className="text-bad">{e.discarded}</span>
                        </>
                      ) : (
                        ''
                      )}
                    </Td>
                  </tr>
                  {expanded && (
                    <tr className="bg-canvas">
                      <td colSpan={7} className="border-b border-rule px-4 py-2.5 sm:px-5">
                        <ul className="space-y-1 text-caption">
                          {e.items.map((v) => (
                            <li key={v.id}>
                              <Link className="font-medium text-ink underline-offset-2 hover:underline" to={`/vacancies?open=${v.id}`}>
                                {v.name}
                              </Link>{' '}
                              <span className={scoreClass(v.score)}>{v.score !== null ? `оценка ${v.score}` : `предоценка ${num(v.prescore)}`}</span>
                              <span className="text-ink-3">
                                {' '}
                                · {v.status} · откликов {num(v.responses)} · {rub(v.salary)} · {v.published_at ? shortAgo(v.published_at) : '—'}
                                {v.archived ? ' · архив' : ''}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </Table>
      )}
    </Panel>
  )
}

function TrendsSection({ qs, slice }: { qs: string; slice: Slice }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['an-trends', qs], queryFn: () => apiGet<TrendsView>(`/api/analytics/trends?weeks=12${qs}`) })
  const retag = useMutation({
    mutationFn: () => apiPost<{ retagged: number }>('/api/analytics/retag'),
    onSuccess: () => void qc.invalidateQueries({ predicate: (query) => String(query.queryKey[0]).startsWith('an-') }),
  })
  const d = q.data
  return (
    <Panel title={sliceTitle('Тренды по неделям', slice)} meta="По дате первого появления вакансии в базе." flush>
      {q.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={q.error} title="Тренды не загрузились" onRetry={() => void q.refetch()} />}
      {q.isLoading && <Skeleton className="mx-5 mb-5 h-40" />}
      {d && (
        <>
          <Table label="Тренды по неделям" density="compact">
            <thead>
              <tr>
                <Th>{'Неделя\u00a0с'}</Th>
                <Th align="right">Новых</Th>
                <Th align="right">Оценено</Th>
                <Th align="right">{'≥\u00a050'}</Th>
                <Th align="right">Отправлено</Th>
                <Th align="right">{'₽\u00a0медиана'}</Th>
                <Th align="right">Откликов, медиана</Th>
                {d.tools.map((t) => (
                  <Th key={t} align="right" title="Вакансий недели с этим инструментом">
                    {t}
                  </Th>
                ))}
              </tr>
            </thead>
            <tbody>
              {d.weeks.map((w) => (
                <tr key={w.week}>
                  <Td className="tabular-nums">{w.week.slice(5).split('-').reverse().join('.')}</Td>
                  <Td align="right">{w.new}</Td>
                  <Td align="right">{w.evaluated}</Td>
                  <Td align="right">{w.ge50}</Td>
                  <Td align="right">{w.sent}</Td>
                  <Td align="right">{rub(w.salary_median)}</Td>
                  <Td align="right">{num(w.responses_median)}</Td>
                  {d.tools.map((t) => (
                    <Td key={t} align="right" className="text-ink-2">
                      {w.tools[t] || ''}
                    </Td>
                  ))}
                </tr>
              ))}
            </tbody>
          </Table>
          <div className="grid gap-5 px-4 py-4 sm:px-5 md:grid-cols-3">
            {(['domain', 'tasks', 'stack'] as const).map((g) => (
              <div key={g}>
                <h3 className="mb-2 text-caption font-medium text-ink-2">{g === 'domain' ? 'Отрасли' : g === 'tasks' ? 'Задачи' : 'Инструменты'}</h3>
                <HBars rows={d.tags[g].map((t) => ({ label: t.name, value: t.count, hint: pct(t.share) }))} />
              </div>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3 text-caption text-ink-3 sm:px-5">
            Метки ставит панель по тексту вакансии (отрасль, задачи, инструменты) и дополняет Claude при оценке.
            <Button variant="ghost" size="sm" onClick={() => retag.mutate()} disabled={retag.isPending}>
              {retag.isPending ? 'Пересчитываю…' : 'Пересчитать метки'}
            </Button>
            {retag.data && <span className="text-good">обновлено {retag.data.retagged}</span>}
            {retag.error && <span className="text-bad">{errorMessage(retag.error)}</span>}
          </div>
        </>
      )}
    </Panel>
  )
}

// ---- funnel of answers ---------------------------------------------------------------------------------------

function FunnelSection({ days }: { days: number }) {
  const [by, setBy] = useState('letter')
  const q = useQuery({ queryKey: ['an-funnel', by, days], queryFn: () => apiGet<FunnelView>(`/api/analytics/funnel?by=${by}&days=${days}`) })
  const d = q.data
  const row = (c: FunnelView['overall'], label: string, bold = false) => (
    <tr key={label} className={cx(bold && 'font-medium')}>
      <Td className="max-w-[16rem] truncate" title={label}>
        {label}
      </Td>
      <Td align="right">{c.sent}</Td>
      <Td align="right">
        {c.viewed} <span className="text-ink-3">{pct(c.viewed_pct)}</span>
      </Td>
      <Td align="right" className="text-good">
        {c.invited} <span className="text-ink-3">{pct(c.invited_pct)}</span>
      </Td>
      <Td align="right" className="text-bad">
        {c.discarded}
      </Td>
      <Td align="right">{pct(c.reacted_pct)}</Td>
      <Td align="right" className="text-ink-3">
        {c.no_answer}
      </Td>
      <Td align="right" className="text-ink-3">
        {c.pending}
      </Td>
    </tr>
  )
  return (
    <Panel
      title="Воронка ответов"
      meta={`По отправленным откликам с датой отправки в периоде; реакции взяты из синхронизации с hh.ru или ручного журнала.${d && !d.enough_data ? ` Пока ${d.overall.sent} откликов: разница между группами станет надёжной примерно от 30.` : ''}`}
      actions={
        <Select aria-label="Группировать по" wrapperClassName="w-auto" value={by} onChange={(e) => setBy(e.target.value)}>
          {[...FUNNEL_EXTRA, ...KINDS].map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </Select>
      }
      flush
    >
      {q.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={q.error} title="Воронка не загрузилась" onRetry={() => void q.refetch()} />}
      {q.isLoading && <Skeleton className="mx-5 mb-5 h-40" />}
      {d && (
        <Table label="Воронка ответов" minWidth={760}>
          <thead>
            <tr>
              <Th>Группа</Th>
              <Th align="right">Отправлено</Th>
              <Th align="right">Просмотрено</Th>
              <Th align="right">Приглашений</Th>
              <Th align="right">Отказов</Th>
              <Th align="right" title="Любая реакция работодателя / отправлено">
                Реакция
              </Th>
              <Th align="right" title="Без реакции больше 7 дней">
                Без ответа
              </Th>
              <Th align="right" title="Отправлено меньше 7 дней назад, реакции пока нет">
                Ждём
              </Th>
            </tr>
          </thead>
          <tbody>
            {row(d.overall, 'Все отклики', true)}
            {d.rows.map((r) => row(r, r.label))}
          </tbody>
        </Table>
      )}
    </Panel>
  )
}
