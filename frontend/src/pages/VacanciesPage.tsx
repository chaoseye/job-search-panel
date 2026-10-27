import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDownUp, ChevronDown, Clock, Link2, MoreHorizontal, SlidersHorizontal } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { apiDelete, apiGet, apiPost } from '../api/client'
import type { AddByLinksResult, ApplicationRow, ApplyStatus, HiddenEmployer, JobsView, QueueResult, SavedSearch, SelectionProfile, VacancyCounts, VacancyList, VacancyRow } from '../api/types'
import AssistPanel from '../components/AssistPanel'
import ConsentModal from '../components/ConsentModal'
import { ClassicFilters, ClassicTable, HiddenEmployersPanel } from '../components/VacanciesClassic'
import VacancyDrawer, { type DrawerPosition } from '../components/VacancyDrawer'
import { Badge, Count } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Chip, ToggleChip } from '../components/ui/Chip'
import { cx } from '../components/ui/cx'
import { Disclosure } from '../components/ui/Disclosure'
import { Checkbox, Field, Input, Select, Textarea } from '../components/ui/Field'
import { Menu, type MenuEntry } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Pagination } from '../components/ui/Pagination'
import { Panel } from '../components/ui/Panel'
import { Popover } from '../components/ui/Popover'
import { SearchInput } from '../components/ui/SearchInput'
import { ScoreChip } from '../components/ui/Score'
import { Segmented } from '../components/ui/Segmented'
import { Sheet, SheetCloseButton } from '../components/ui/Sheet'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Tabs } from '../components/ui/Tabs'
import { useMediaQuery } from '../components/ui/useMediaQuery'
import { countOf, formatDateTime, plural, relativeTime, shortAgo, shortDate } from '../lib/format'
import { FILTERS_KEY, isGroup, LAYOUT_KEY, loadFilters, loadLayout, type Filters, type VacancyLayout } from '../lib/vacancyFilters'
import {
  clearFilters,
  EXPERIENCE_LEVELS,
  experienceShort,
  filterChips,
  formatsShort,
  GROUP_LABEL,
  MORE_GROUPS,
  PRIMARY_GROUPS,
  rowFlags,
  rowStatus,
  salaryNet,
  salaryPublished,
  SORTS,
  WORK_FORMATS,
} from '../lib/vacancyView'

const PER_PAGE = 50
const SCORE_PRESETS = ['', '50', '60', '70']
const LAYOUTS: [VacancyLayout, string][] = [
  ['compact', 'Компактный'],
  ['classic', 'Классический'],
]

const EMPTY_TEXT: Record<string, [string, string]> = {
  candidates: ['Кандидатов пока нет', 'Новые вакансии появятся после следующей проверки поисков, а оценки появятся после разбора в Claude.'],
  needs_clarification: ['Уточнять нечего', 'Во всех письмах, которые ещё могут уйти, нет вопросов к вам.'],
  needs_manual: ['Ничего не ждёт действий на hh.ru', 'Здесь появятся вакансии, отклик на которые вы открыли, но ещё не подтвердили.'],
  sent: ['Откликов пока нет', 'Отправленные отклики появятся здесь.'],
}

/** «Вакансии» in two views of one list. Compact: the tabs of the pipeline, a search field, the filters in a popover (and
 *  as chips while they hide something), a dense list. Classic (from 768 px): the structure from before the redesign —
 *  every group as a button, all the filters open, a table (components/VacanciesClassic). The card of the vacancy in a
 *  side sheet, the data and the actions are the same in both. */
export default function VacanciesPage() {
  const qc = useQueryClient()
  const [filters, setFilters] = useState<Filters>(loadFilters)
  const [layout, setLayout] = useState<VacancyLayout>(loadLayout)
  // the classic table needs width: a phone keeps the compact view and the choice waits for a wider screen
  const phone = useMediaQuery('(max-width: 767px)')
  const classic = layout === 'classic' && !phone
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<number | null>(null)
  const [checked, setChecked] = useState<number[]>([])
  // /vacancies?open=<id> (links from «Аналитика») opens the card right away; ?group=<id> (the dashboard block
  // «Письма ждут уточнений») switches the tab to that group
  const [urlParams, setUrlParams] = useSearchParams()
  useEffect(() => {
    const open = urlParams.get('open')
    const group = urlParams.get('group')
    if (!open && !group) return
    if (open && /^\d+$/.test(open)) setSelected(Number(open))
    if (isGroup(group)) {
      setFilters((f) => ({ ...f, group }))
      setPage(0)
    }
    urlParams.delete('open')
    urlParams.delete('group')
    setUrlParams(urlParams, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlParams])
  const [showConsent, setShowConsent] = useState<null | (() => void)>(null)
  const [assistQueue, setAssistQueue] = useState<number[]>([])
  const [assistCurrent, setAssistCurrent] = useState<ApplicationRow | null>(null)
  const [queueResult, setQueueResult] = useState<QueueResult | null>(null)
  const [confirmBatch, setConfirmBatch] = useState(false)
  const [hiddenOpen, setHiddenOpen] = useState(false)
  const narrow = useMediaQuery('(max-width: 639px)')
  const applyStatus = useQuery({ queryKey: ['apply-status'], queryFn: () => apiGet<ApplyStatus>('/api/applications/status') })
  useEffect(() => {
    try {
      localStorage.setItem(FILTERS_KEY, JSON.stringify(filters))
    } catch {
      /* ignore */
    }
  }, [filters])
  useEffect(() => {
    try {
      localStorage.setItem(LAYOUT_KEY, layout)
    } catch {
      /* ignore */
    }
  }, [layout])

  const params = new URLSearchParams()
  params.set('group', filters.group)
  if (filters.min_score) params.set('min_score', filters.min_score)
  if (filters.search_id) params.set('search_id', filters.search_id)
  if (filters.selection_profile_id) params.set('selection_profile_id', filters.selection_profile_id)
  if (filters.area) params.set('area', filters.area)
  if (filters.work_format) params.set('work_format', filters.work_format)
  if (filters.experience) params.set('experience', filters.experience)
  if (filters.salary_from) params.set('salary_from', filters.salary_from)
  if (filters.q) params.set('q', filters.q)
  if (filters.include_snoozed) params.set('include_snoozed', 'true')
  if (filters.unseen_only) params.set('unseen_only', 'true')
  params.set('sort', filters.sort)
  params.set('page', String(page))
  params.set('per_page', String(PER_PAGE))
  const key = params.toString()

  const list = useQuery({ queryKey: ['vacancies', key], queryFn: () => apiGet<VacancyList>(`/api/vacancies?${key}`), refetchInterval: 30000 })
  const searches = useQuery({ queryKey: ['searches'], queryFn: () => apiGet<SavedSearch[]>('/api/searches') })
  const sets = useQuery({ queryKey: ['selection-profiles'], queryFn: () => apiGet<{ items: SelectionProfile[] }>('/api/selection-profiles') })
  const hiddenEmployers = useQuery({ queryKey: ['hidden-employers'], queryFn: () => apiGet<HiddenEmployer[]>('/api/vacancies/hidden-employers') })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['vacancies'] })
    void qc.invalidateQueries({ queryKey: ['vacancy-summary'] })
    void qc.invalidateQueries({ queryKey: ['hidden-employers'] })
    void qc.invalidateQueries({ queryKey: ['applications'] })
    void qc.invalidateQueries({ queryKey: ['apply-status'] })
  }
  const markSeen = useMutation({ mutationFn: () => apiPost('/api/vacancies/mark-seen', {}), onSuccess: invalidate })
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => apiGet<JobsView>('/api/jobs?limit=5'), refetchInterval: 5000 })
  const reprocessAll = useMutation({
    mutationFn: (cancelPending: boolean) => apiPost<{ job_run_id: number }>('/api/vacancies/reprocess-all', { cancel_pending: cancelPending }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['jobs'] })
      void qc.invalidateQueries({ queryKey: ['batches'] })
      void qc.invalidateQueries({ queryKey: ['batches-summary'] })
      setTimeout(invalidate, 3000)
    },
  })
  const reprocessing = jobs.data?.running.includes('reprocess') ?? false
  const scanning = jobs.data?.running.includes('scan') ?? false
  const lastReprocess = jobs.data?.runs.find((r) => r.job === 'reprocess')
  const queueApply = useMutation({
    mutationFn: (ids: number[]) => apiPost<QueueResult>('/api/applications/queue', { vacancy_ids: ids }),
    onSuccess: (r) => {
      setQueueResult(r)
      setChecked([])
      invalidate()
    },
  })
  const assistOpen = useMutation({
    mutationFn: (vacancy_id: number) => apiPost<ApplicationRow>('/api/applications/assist/open', { vacancy_id }),
    onSuccess: (r) => {
      setAssistCurrent(r)
      invalidate()
    },
  })
  const needsConsent = applyStatus.data?.consent_accepted_at === null
  const withConsent = (fn: () => void) => {
    if (needsConsent) setShowConsent(() => fn)
    else fn()
  }
  const startAssist = (ids: number[]) => {
    if (!ids.length) return
    const [first, ...rest] = ids
    setAssistQueue(rest)
    assistOpen.mutate(first)
  }
  const assistNext = () => {
    setAssistCurrent(null)
    if (assistQueue.length) startAssist(assistQueue)
    else setAssistQueue([])
  }
  const mode = applyStatus.data?.apply_mode ?? 'assist'
  const applySelected = () =>
    withConsent(() => {
      if (mode === 'api') setConfirmBatch(true)
      else {
        startAssist(checked)
        setChecked([])
      }
    })
  const toggleChecked = (id: number) => setChecked((c) => (c.includes(id) ? c.filter((x) => x !== id) : [...c, id]))
  const unhideEmployer = useMutation({ mutationFn: (id: number) => apiDelete(`/api/vacancies/hidden-employers/${id}`), onSuccess: invalidate })

  const set = (patch: Partial<Filters>) => {
    setFilters((f) => ({ ...f, ...patch }))
    setPage(0)
  }
  const replaceFilters = (next: Filters) => {
    setFilters(next)
    setPage(0)
  }
  const reprocess = () => {
    if (!window.confirm('Заново прогнать все необработанные, отфильтрованные и оценённые вакансии через текущие правила и оценку? Отправленные и пропущенные не затрагиваются.')) return
    const awaiting = counts?.awaiting_claude ?? 0
    const cancelPending =
      awaiting > 0 && window.confirm(`${awaiting} вакансий уже лежат в пакетах для /hh-batch. Отменить эти пакеты и переоценить их тоже? («Отмена» — пакеты остаются как есть.)`)
    reprocessAll.mutate(cancelPending)
  }

  const counts: VacancyCounts | undefined = list.data?.counts
  const items = list.data?.items ?? []
  const total = list.data?.total ?? 0
  const groupCount = (id: string): number | undefined => (counts ? (id === 'all' ? counts.total : (counts as unknown as Record<string, number>)[id]) : undefined)
  const chips = filterChips(filters, searches.data, sets.data?.items)
  const narrowed = chips.length > 0 || Boolean(filters.q)
  const hiddenCount = hiddenEmployers.data?.length ?? 0

  // the card walks the list it was opened from; a vacancy that left the list (skipped, hidden) keeps its place
  const ids = items.map((v) => v.id)
  const lastIndex = useRef(0)
  const at = selected === null ? -1 : ids.indexOf(selected)
  if (at >= 0) lastIndex.current = at
  let position: DrawerPosition | undefined
  if (selected !== null && ids.length) {
    const i = at >= 0 ? at : Math.min(lastIndex.current, ids.length - 1)
    const prevId = ids[i - 1]
    const nextId = at >= 0 ? ids[i + 1] : ids[i]
    position = {
      index: i,
      total: ids.length,
      onPrev: prevId !== undefined ? () => setSelected(prevId) : undefined,
      onNext: nextId !== undefined ? () => setSelected(nextId) : undefined,
    }
  }
  useEffect(() => {
    if (selected === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return
      // typing in a field, a menu or a popover keeps its letters; the card itself (labelled by its title) does not
      const t = e.target
      if (t instanceof Element && t.closest('input, textarea, select, [contenteditable="true"], [role="menu"], [role="dialog"][aria-label]')) return
      if (e.code === 'KeyJ') position?.onNext?.()
      else if (e.code === 'KeyK') position?.onPrev?.()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const meta: ReactNode[] = []
  if (counts) {
    meta.push(countOf(counts.candidates, ['кандидат', 'кандидата', 'кандидатов']))
    if (counts.unseen_candidates > 0) meta.push(`${counts.unseen_candidates} ${counts.unseen_candidates === 1 ? 'новый' : 'новых'}`)
    if (counts.new > 0) meta.push(`${counts.new} ${plural(counts.new, ['ждёт', 'ждут', 'ждут'])} обработки`)
  }
  if (jobs.data?.last_scan_at) meta.push(`поиски проверены ${relativeTime(jobs.data.last_scan_at)}`)

  const moreItems: MenuEntry[] = MORE_GROUPS.flatMap(({ section, ids: groupIds }) => [
    { section },
    ...groupIds.map((id) => ({
      key: id,
      label: (
        <span className="flex items-center justify-between gap-6">
          <span>{GROUP_LABEL[id] ?? id}</span>
          <span className="text-xs tabular-nums text-ink-3">{groupCount(id) ?? ''}</span>
        </span>
      ),
      checked: filters.group === id,
      onSelect: () => {
        set({ group: id })
        setChecked([])
      },
    })),
  ])
  const inMore = !PRIMARY_GROUPS.some((g) => g.id === filters.group)
  const selectGroup = (g: string) => {
    set({ group: g })
    setChecked([])
  }
  const shown = list.data
    ? narrowed && groupCount(filters.group) !== undefined && total < (groupCount(filters.group) ?? 0)
      ? `Показано ${total.toLocaleString('ru-RU')} из ${groupCount(filters.group)!.toLocaleString('ru-RU')}`
      : countOf(total, ['вакансия', 'вакансии', 'вакансий'])
    : null
  const listError = list.error ? <ErrorNotice className="m-4" error={list.error} title="Не удалось загрузить вакансии" onRetry={() => void list.refetch()} /> : null
  const emptyState =
    list.data && items.length === 0 ? (
      <EmptyState
        title={narrowed ? 'Ничего не найдено' : (EMPTY_TEXT[filters.group]?.[0] ?? 'Здесь пусто')}
        action={
          narrowed ? (
            <Button onClick={() => replaceFilters(clearFilters(filters))}>Сбросить фильтры и поиск</Button>
          ) : filters.group === 'candidates' ? (
            <Link className="text-sm font-medium text-accent underline-offset-2 hover:underline" to="/searches">
              Открыть поиски
            </Link>
          ) : undefined
        }
      >
        {narrowed
          ? `В группе «${GROUP_LABEL[filters.group] ?? filters.group}» ${countOf(groupCount(filters.group) ?? 0, ['вакансия', 'вакансии', 'вакансий'])}, но поиск и фильтры скрывают все.`
          : EMPTY_TEXT[filters.group]?.[1]}
      </EmptyState>
    ) : null
  const layoutSwitch = !phone && <Segmented label="Вид страницы" options={LAYOUTS} value={layout} onChange={setLayout} />
  const applyLabel = mode === 'api' ? `Откликнуться (${checked.length})` : `Открыть по очереди на hh.ru (${checked.length})`

  return (
    <div className="flex flex-col gap-5">
      {classic ? (
        <PageHeader
          title="Вакансии"
          meta={meta.join(' · ')}
          actions={
            <>
              {layoutSwitch}
              {checked.length > 0 && (
                <>
                  <span className="text-sm font-medium">Выбрано {checked.length}</span>
                  <Button variant="primary" onClick={applySelected} disabled={queueApply.isPending || assistOpen.isPending}>
                    {applyLabel}
                  </Button>
                  <Button variant="ghost" onClick={() => setChecked([])}>
                    Снять выбор
                  </Button>
                </>
              )}
              <Button
                onClick={reprocess}
                disabled={reprocessAll.isPending || reprocessing || scanning}
                title={lastReprocess ? `Последняя переоценка ${formatDateTime(lastReprocess.started_at)}` : 'Правила и предоценка заново по текущей анкете'}
              >
                {reprocessing ? 'Переоценка идёт…' : 'Переоценить всё по критериям'}
              </Button>
              <Button onClick={() => markSeen.mutate()} disabled={markSeen.isPending || !counts?.unseen_candidates}>
                Отметить всё просмотренным
              </Button>
            </>
          }
        />
      ) : (
      <PageHeader
        title="Вакансии"
        meta={meta.join(' · ')}
        actions={
          <>
            {layoutSwitch}
            <AddByLinks sets={sets.data?.items ?? []} onAdded={invalidate} />
            <Button variant="ghost" className="max-sm:hidden" onClick={() => markSeen.mutate()} disabled={markSeen.isPending || !counts?.unseen_candidates}>
              Отметить всё просмотренным
            </Button>
            <Menu
              label="Ещё действия со списком"
              items={[
                ...(narrow ? [{ key: 'seen', label: 'Отметить всё просмотренным', disabled: markSeen.isPending || !counts?.unseen_candidates, onSelect: () => markSeen.mutate() }] : []),
                {
                  key: 'reprocess',
                  label: reprocessing ? 'Переоценка идёт…' : 'Переоценить всё по критериям',
                  hint: lastReprocess ? `последний раз ${formatDateTime(lastReprocess.started_at)}` : 'правила и предоценка заново по текущей анкете',
                  disabled: reprocessAll.isPending || reprocessing || scanning,
                  onSelect: reprocess,
                },
                { key: 'hidden', label: `Скрытые работодатели${hiddenCount ? ` (${hiddenCount})` : ''}`, disabled: !hiddenCount, onSelect: () => setHiddenOpen(true) },
              ]}
              trigger={(p) => (
                <Button {...p} variant="ghost" iconOnly aria-label="Ещё действия со списком" title="Ещё действия">
                  <MoreHorizontal aria-hidden />
                </Button>
              )}
            />
          </>
        }
      />
      )}

      {(queueApply.error || assistOpen.error || reprocessAll.error) && <ErrorNotice error={queueApply.error ?? assistOpen.error ?? reprocessAll.error} title="Действие не выполнено" />}
      {classic && (
        <Panel>
          <Disclosure summary="Добавить вакансии по ссылкам">
            <AddByLinksForm sets={sets.data?.items ?? []} onAdded={invalidate} className="max-w-2xl" />
          </Disclosure>
        </Panel>
      )}
      {reprocessing && <Notice tone="info" role="status" title="Идёт переоценка по критериям">Список обновится сам, когда она закончится.</Notice>}
      {confirmBatch && checked.length > 0 && (
        <Notice
          tone="info"
          title={`Отправить ${countOf(checked.length, ['отклик', 'отклика', 'откликов'])} через API hh.ru?`}
          action={
            <div className="flex gap-2">
              <Button
                variant="primary"
                size="sm"
                disabled={queueApply.isPending}
                onClick={() => {
                  setConfirmBatch(false)
                  queueApply.mutate(checked)
                }}
              >
                Да, отправить
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirmBatch(false)}>
                Отмена
              </Button>
            </div>
          }
        >
          <ul className="mt-1 list-disc pl-4">
            {items
              .filter((v) => checked.includes(v.id))
              .map((v) => (
                <li key={v.id}>
                  {v.name} — {v.employer.name ?? '—'}
                </li>
              ))}
          </ul>
          <p className="mt-1">
            Отклики уходят по одному с паузой, в пределах дневного и часового лимитов.{applyStatus.data?.dry_run ? ' Включён сухой прогон: на hh.ru ничего не отправится.' : ''}
          </p>
        </Notice>
      )}
      {queueResult && (
        <Notice
          tone={queueResult.rejected.length ? 'warn' : 'good'}
          role="status"
          title={`В очередь поставлено: ${queueResult.queued.length}${queueResult.job_started ? ' · отправка началась' : ''}`}
          action={
            <Button size="sm" variant="ghost" onClick={() => setQueueResult(null)}>
              Скрыть
            </Button>
          }
        >
          {queueResult.rejected.length > 0 && (
            <ul className="list-disc pl-4">
              {queueResult.rejected.map((r) => (
                <li key={r.vacancy_id}>
                  {r.name ?? `#${r.vacancy_id}`}: {r.reason}
                </li>
              ))}
            </ul>
          )}
        </Notice>
      )}
      {assistCurrent && (
        <AssistPanel
          application={assistCurrent}
          remaining={assistQueue.length}
          onDone={() => {
            invalidate()
            assistNext()
          }}
          onNext={assistQueue.length ? assistNext : undefined}
        />
      )}
      {showConsent && (
        <ConsentModal
          onAccepted={() => {
            const fn = showConsent
            setShowConsent(null)
            fn()
          }}
          onClose={() => setShowConsent(null)}
        />
      )}

      {classic ? (
        <section aria-label="Список вакансий" className="flex flex-col gap-5">
          <ClassicFilters
            filters={filters}
            set={set}
            onGroup={selectGroup}
            onReset={() => replaceFilters(clearFilters(filters))}
            canReset={narrowed}
            count={groupCount}
            searches={searches.data ?? []}
            sets={sets.data?.items ?? []}
            snoozed={counts?.snoozed ?? 0}
            summary={shown}
          />
          <ClassicTable
            items={items}
            loading={list.isLoading}
            notice={listError}
            empty={emptyState}
            checked={checked}
            onToggle={toggleChecked}
            onToggleAll={(on) => setChecked(on ? items.map((v) => v.id) : [])}
            selected={selected}
            onOpen={setSelected}
            page={page}
            perPage={PER_PAGE}
            total={total}
            onPage={setPage}
          />
        </section>
      ) : (
      <section aria-label="Список вакансий">
        <Tabs
          label="Группы вакансий"
          value={inMore ? '' : filters.group}
          onChange={selectGroup}
          items={PRIMARY_GROUPS.map((g) => ({ id: g.id, label: g.label, count: groupCount(g.id), attention: g.attention }))}
          trailing={
            <Menu
              label="Другие группы"
              items={moreItems}
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
                  {inMore ? GROUP_LABEL[filters.group] : 'Ещё'}
                  {inMore && groupCount(filters.group) !== undefined && <Count value={groupCount(filters.group)!} />}
                  <ChevronDown aria-hidden className="size-4 text-ink-3" />
                </button>
              )}
            />
          }
        />

        {checked.length > 0 ? (
          <div role="toolbar" aria-label="Действия с выбранными" className="flex flex-wrap items-center gap-2 py-3">
            <span className="text-sm font-medium">Выбрано {checked.length}</span>
            <Button variant="primary" onClick={applySelected} disabled={queueApply.isPending || assistOpen.isPending}>
              {applyLabel}
            </Button>
            <Button variant="ghost" onClick={() => setChecked([])}>
              Снять выбор
            </Button>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2 py-3">
            <SearchInput label="Поиск по названию или компании" placeholder="Название или компания" value={filters.q} onChange={(q) => set({ q })} />
            <FiltersPopover filters={filters} set={set} onReset={() => replaceFilters(clearFilters(filters))} active={chips.length} searches={searches.data ?? []} sets={sets.data?.items ?? []} snoozed={counts?.snoozed ?? 0} />
            <Menu
              label="Сортировка"
              align="start"
              items={SORTS.map(([id, label]) => ({ key: id, label, checked: filters.sort === id, onSelect: () => set({ sort: id }) }))}
              trigger={(p) => (
                <Button {...p} variant="ghost">
                  <ArrowDownUp aria-hidden />
                  {SORTS.find(([id]) => id === filters.sort)?.[1]}
                </Button>
              )}
            />
            {shown && (
              <span className="ml-auto text-caption tabular-nums text-ink-3" aria-live="polite">
                {shown}
              </span>
            )}
          </div>
        )}

        {chips.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 pb-3" aria-label="Действующие фильтры">
            {chips.map((c) => (
              <Chip key={c.key} onRemove={() => replaceFilters(c.clear(filters))} removeLabel={`Убрать фильтр «${c.label}»`}>
                {c.label}
              </Chip>
            ))}
            <Button variant="ghost" size="sm" onClick={() => replaceFilters(clearFilters(filters))}>
              Сбросить все
            </Button>
          </div>
        )}

        <div className="overflow-clip rounded-lg border border-line bg-surface">
          <div className="vrow vrow-head sticky top-12 z-10 hidden border-b border-line bg-surface px-4 py-2 text-xs font-medium text-ink-3 md:grid lg:top-0">
            <span className="[grid-area:check]">
              {items.length > 0 && (
                <input
                type="checkbox"
                className="size-4 cursor-pointer accent-accent"
                checked={items.length > 0 && items.every((v) => checked.includes(v.id))}
                onChange={(e) => setChecked(e.target.checked ? items.map((v) => v.id) : [])}
                aria-label="Выбрать все на странице"
                title="Выбрать все на странице"
              />
              )}
            </span>
            <span className="[grid-area:score]">Оценка</span>
            <span className="[grid-area:title]">Вакансия</span>
            <span className="text-right [grid-area:salary]">На руки</span>
            <span className="hidden text-right [grid-area:age] xl:block">Опубл.</span>
            <span className="hidden text-right [grid-area:responses] xl:block">Откликов</span>
            <span className="[grid-area:status]">Статус</span>
          </div>

          {list.isLoading && <ListSkeleton />}
          {listError}
          {emptyState}
          {items.length > 0 && (
            <ul aria-label="Вакансии">
              {items.map((v) => (
                <VacancyListRow key={v.id} v={v} active={selected === v.id} checked={checked.includes(v.id)} onToggle={() => toggleChecked(v.id)} onOpen={() => setSelected(v.id)} />
              ))}
            </ul>
          )}
          <Pagination page={page} perPage={PER_PAGE} total={total} onPage={setPage} />
        </div>
      </section>
      )}

      {classic && hiddenCount > 0 && <HiddenEmployersPanel items={hiddenEmployers.data ?? []} onUnhide={(id) => unhideEmployer.mutate(id)} busy={unhideEmployer.isPending} />}

      {selected !== null && (
        <VacancyDrawer
          id={selected}
          onClose={() => setSelected(null)}
          onChanged={invalidate}
          applyMode={mode}
          position={position}
          onApply={(id) =>
            withConsent(() => {
              if (mode === 'api') queueApply.mutate([id])
              else startAssist([id])
              setSelected(null)
            })
          }
        />
      )}

      {hiddenOpen && (
        <Sheet size="md" labelledBy="hidden-employers-title" onClose={() => setHiddenOpen(false)}>
          <div className="flex h-12 shrink-0 items-center justify-between border-b border-line pl-5 pr-3">
            <h2 id="hidden-employers-title" className="text-[15px] font-semibold">
              Скрытые работодатели
            </h2>
            <SheetCloseButton />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-3">
            {hiddenCount === 0 ? (
              <EmptyState inset="inline" title="Скрытых работодателей нет" />
            ) : (
              <ul className="divide-y divide-rule">
                {hiddenEmployers.data?.map((h) => (
                  <li key={h.id} className="flex items-center justify-between gap-3 py-2.5">
                    <span className="min-w-0 truncate text-sm">{h.employer_name || h.employer_id}</span>
                    <Button variant="ghost" size="sm" disabled={unhideEmployer.isPending} onClick={() => unhideEmployer.mutate(h.id)}>
                      Показывать снова
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-4 text-caption text-ink-3">Вакансии скрытого работодателя не попадают в кандидаты. Если снова показывать, вакансии, скрытые вместе с ним, вернутся.</p>
          </div>
        </Sheet>
      )}
    </div>
  )
}

function Sep() {
  return <span aria-hidden className="text-ink-3"> · </span>
}

function VacancyListRow({ v, active, checked, onToggle, onOpen }: { v: VacancyRow; active: boolean; checked: boolean; onToggle: () => void; onOpen: () => void }) {
  const status = rowStatus(v)
  const net = salaryNet(v.salary)
  const published = salaryPublished(v.salary)
  const formats = formatsShort(v)
  const exp = experienceShort(v.experience_name)
  const flags = rowFlags(v.flags)
  const place = [v.address, v.metro.length ? `м. ${v.metro.join(', ')}` : ''].filter(Boolean).join(' · ')
  const age = shortAgo(v.published_at)
  return (
    <li
      onClick={onOpen}
      className={cx(
        'vrow cursor-pointer border-b border-rule px-4 py-2.5 transition-colors last:border-b-0',
        active ? 'bg-accent-soft' : 'hover:bg-sunken',
      )}
    >
      <span className="hidden [grid-area:check] md:block" onClick={(e) => e.stopPropagation()}>
        <input type="checkbox" className="size-4 cursor-pointer accent-accent" checked={checked} onChange={onToggle} aria-label={`Выбрать: ${v.name}`} />
      </span>
      <span className="pt-px [grid-area:score] md:pt-0">
        <ScoreChip score={v.score} prescore={v.prescore} />
      </span>
      <span className="flex min-w-0 items-start gap-2 [grid-area:title] md:items-center">
        {v.is_new && (
          <span className="mt-2 size-1.5 shrink-0 rounded-full bg-accent md:mt-0" title="Новая, ещё не просмотрена">
            <span className="sr-only">Новая.</span>
          </span>
        )}
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation()
            onOpen()
          }}
          className="min-w-0 rounded-sm text-left text-sm font-medium text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus max-md:line-clamp-2 md:truncate"
          title={v.name}
        >
          {v.name}
        </button>
        {v.duplicate_of_id !== null && (
          <Badge tone="outline" title="Повторная публикация вакансии, которая уже есть в базе">
            дубль
          </Badge>
        )}
      </span>
      <span className="min-w-0 truncate text-caption text-ink-2 [grid-area:meta]" title={place || undefined}>
        <span className="text-ink">{v.employer.name ?? 'Работодатель не указан'}</span>
        {v.employer.accredited_it && (
          <span className="ml-1 text-xs font-medium text-ink-3" title="Аккредитованная IT-компания">
            IT
          </span>
        )}
        {v.area_name && (
          <>
            <Sep />
            {v.area_name}
          </>
        )}
        {formats && (
          <>
            <Sep />
            {formats}
          </>
        )}
        {v.metro.length > 0 && (
          <>
            <Sep />
            м. {v.metro[0]}
            {v.metro.length > 1 && <span className="text-ink-3"> +{v.metro.length - 1}</span>}
          </>
        )}
        {exp && (
          <>
            <Sep />
            {exp}
          </>
        )}
        {flags.map((f) => (
          <span key={f.key}>
            <Sep />
            <span className="text-warn">{f.label}</span>
          </span>
        ))}
        <span className="hidden md:inline xl:hidden">
          <Sep />
          {age}
          {v.responses_count !== null && (
            <>
              <Sep />
              {v.responses_count} откл.
            </>
          )}
        </span>
      </span>
      <span className="text-caption tabular-nums text-ink-2 [grid-area:facts] md:hidden">
        {net ? <span className="font-medium text-ink">{net}</span> : <span className="text-ink-3">без зарплаты</span>}
        <Sep />
        {age}
        {v.responses_count !== null && (
          <>
            <Sep />
            {v.responses_count} откл.
          </>
        )}
      </span>
      <span className="hidden min-w-0 text-right [grid-area:salary] md:block" title={published ?? undefined}>
        {net ? <span className="block text-sm font-medium tabular-nums text-ink">{net}</span> : <span className="block text-sm text-ink-3">—</span>}
        {published && <span className="block truncate text-xs text-ink-3">{published}</span>}
      </span>
      <span className="hidden text-right text-caption tabular-nums text-ink-2 [grid-area:age] xl:block" title={formatDateTime(v.published_at)}>
        {age}
      </span>
      <span className="hidden text-right text-caption tabular-nums text-ink-2 [grid-area:responses] xl:block">{v.responses_count ?? '—'}</span>
      <span className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 [grid-area:status] md:mt-0 md:flex-col md:items-start md:gap-0.5">
        <Badge tone={status.tone}>{status.label}</Badge>
        {v.snoozed_until && (
          <span className="inline-flex items-center gap-1 text-xs text-ink-3">
            <Clock aria-hidden className="size-3" />
            до {shortDate(v.snoozed_until)}
          </span>
        )}
      </span>
    </li>
  )
}

function ListSkeleton() {
  return (
    <div aria-busy="true" aria-label="Загрузка списка">
      {Array.from({ length: 8 }, (_, i) => (
        <div key={i} className="vrow border-b border-rule px-4 py-3 last:border-b-0">
          <span className="hidden [grid-area:check] md:block" />
          <Skeleton className="h-6 w-9 [grid-area:score]" />
          <Skeleton className="h-4 w-3/5 [grid-area:title]" />
          <Skeleton className="mt-1 h-3 w-2/5 [grid-area:meta]" />
          <Skeleton className="ml-auto hidden h-4 w-20 [grid-area:salary] md:block" />
          <Skeleton className="h-5 w-24 [grid-area:status]" />
        </div>
      ))}
    </div>
  )
}

function Group({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <fieldset className="min-w-0 space-y-1.5">
      <legend className="mb-1.5 text-caption font-medium text-ink-2">{label}</legend>
      {children}
      {hint && <p className="text-xs text-ink-3">{hint}</p>}
    </fieldset>
  )
}

function FiltersPopover({
  filters,
  set,
  onReset,
  active,
  searches,
  sets,
  snoozed,
}: {
  filters: Filters
  set: (patch: Partial<Filters>) => void
  onReset: () => void
  active: number
  searches: SavedSearch[]
  sets: SelectionProfile[]
  snoozed: number
}) {
  const chosen = filters.experience.split(',').filter(Boolean)
  const scoreOptions: [string, string][] = SCORE_PRESETS.map((s) => [s, s ? `от ${s}` : 'Любая'])
  if (filters.min_score && !SCORE_PRESETS.includes(filters.min_score)) scoreOptions.push([filters.min_score, `от ${filters.min_score}`])
  return (
    <Popover
      label="Фильтры"
      className="w-[640px]"
      trigger={(p) => (
        <Button {...p}>
          <SlidersHorizontal aria-hidden />
          Фильтры
          {active > 0 && <span className="ml-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-ink px-1.5 text-xs tabular-nums text-surface">{active}</span>}
        </Button>
      )}
    >
      {(close) => (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-[15px] font-semibold">Фильтры</h2>
            {active > 0 && (
              <Button variant="ghost" size="sm" onClick={onReset}>
                Сбросить
              </Button>
            )}
          </div>
          <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
            <div className="space-y-4">
              <Field label="Поиск, который нашёл вакансию">
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
                <Field label="Набор критериев">
                  <Select value={filters.selection_profile_id} onChange={(e) => set({ selection_profile_id: e.target.value })}>
                    <option value="">Любой</option>
                    <option value="0">Основная анкета</option>
                    {sets.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                  </Select>
                </Field>
              )}
              <div className="grid grid-cols-2 gap-3">
                <Field label="Регион">
                  <Input value={filters.area} onChange={(e) => set({ area: e.target.value })} placeholder="Москва" />
                </Field>
                <Field label="На руки от, ₽">
                  {/* digits only: «-5» or «1.5» would be a 422 from the list and a saved filter loadFilters throws away */}
                  <Input
                    type="number"
                    inputMode="numeric"
                    min={0}
                    step={10000}
                    value={filters.salary_from}
                    onChange={(e) => /^\d*$/.test(e.target.value) && set({ salary_from: e.target.value })}
                    placeholder="150000"
                  />
                </Field>
              </div>
            </div>
            <div className="space-y-4">
              <Group label="Формат работы">
                <Segmented label="Формат работы" options={WORK_FORMATS} value={filters.work_format} onChange={(v) => set({ work_format: v })} />
              </Group>
              <Group label="Требуемый опыт" hint="Можно выбрать несколько.">
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
              </Group>
              <Group label="Оценка">
                <Segmented label="Оценка" options={scoreOptions} value={filters.min_score} onChange={(v) => set({ min_score: v })} />
              </Group>
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
            <div className="flex flex-wrap gap-x-5 gap-y-2">
              <Checkbox label="Только новые" checked={filters.unseen_only} onChange={(e) => set({ unseen_only: e.target.checked })} />
              <Checkbox label={`Показывать отложенные${snoozed ? ` (${snoozed})` : ''}`} checked={filters.include_snoozed} onChange={(e) => set({ include_snoozed: e.target.checked })} />
            </div>
            <Button variant="primary" onClick={close}>
              Готово
            </Button>
          </div>
        </div>
      )}
    </Popover>
  )
}

function AddByLinks({ sets, onAdded }: { sets: SelectionProfile[]; onAdded: () => void }) {
  return (
    <Popover
      label="Добавить вакансии по ссылкам"
      align="end"
      className="w-[440px]"
      trigger={(p) => (
        <Button {...p}>
          <Link2 aria-hidden />
          По ссылке
        </Button>
      )}
    >
      {() => (
        <div className="space-y-3">
          <h2 className="text-[15px] font-semibold">Добавить вакансии по ссылкам</h2>
          <AddByLinksForm sets={sets} onAdded={onAdded} />
        </div>
      )}
    </Popover>
  )
}

/** Links of hh.ru → vacancies and a packet for Claude: in the popover «По ссылке» of the compact view and folded at the
 *  top of the classic one. */
function AddByLinksForm({ sets, onAdded, className }: { sets: SelectionProfile[]; onAdded: () => void; className?: string }) {
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [setId, setSetId] = useState('')
  const add = useMutation({
    mutationFn: () => apiPost<AddByLinksResult>('/api/vacancies/add', { text, selection_profile_id: setId ? Number(setId) : null, create_packet: true }),
    onSuccess: (r) => {
      if (r.added.length) setText('')
      onAdded()
      void qc.invalidateQueries({ queryKey: ['batches'] })
      void qc.invalidateQueries({ queryKey: ['batches-summary'] })
    },
  })
  return (
    <div className={cx('space-y-3', className)}>
      <p className="text-caption text-ink-2">
        Ссылки hh.ru вставляйте по одной в строке или через пробел. Панель загрузит карточки мимо стоп-слов и порога оценки и сразу соберёт пакет для Claude: письмо
        появится в карточке после разбора.
      </p>
      <Textarea aria-label="Ссылки на вакансии" data-autofocus className="min-h-28 font-mono text-xs" placeholder="https://hh.ru/vacancy/123456789" value={text} onChange={(e) => setText(e.target.value)} />
      {sets.length > 0 && (
        <Field label="Критерии отбора">
          <Select value={setId} onChange={(e) => setSetId(e.target.value)}>
            <option value="">Основная анкета</option>
            {sets.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
                {s.resume_name ? ` (резюме ${s.resume_name})` : ''}
              </option>
            ))}
          </Select>
        </Field>
      )}
      <div className="flex justify-end">
        <Button variant="primary" disabled={add.isPending || text.trim().length < 5} onClick={() => add.mutate()}>
          {add.isPending ? 'Загружаю…' : 'Добавить и отправить в разбор'}
        </Button>
      </div>
      {add.error && <ErrorNotice error={add.error} title="Не удалось добавить" />}
      {add.data && (
        <div className="space-y-1 text-caption">
          {add.data.added.map((a) => (
            <div key={a.id} className="text-good">
              ✓ {a.name} — {a.employer ?? '—'} · предоценка {a.prescore ?? '—'} · {a.created ? 'новая' : 'уже была в базе'}
            </div>
          ))}
          {add.data.errors.map((e, i) => (
            <div key={i} className="text-bad">
              ✗ {e.url ? `${e.url}: ` : ''}
              {e.error}
            </div>
          ))}
          {add.data.batch_ids.length > 0 && <div className="text-ink-2">Пакет {add.data.batch_ids.join(', ')} создан, его разберёт /hh-batch.</div>}
        </div>
      )}
    </div>
  )
}
