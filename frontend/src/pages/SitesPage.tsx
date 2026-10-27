import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, MoreHorizontal, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { apiGet, apiPost, apiPut, errorMessage } from '../api/client'
import type { CompanySite, SiteVacancy, SiteVacancyList, SitesView } from '../api/types'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { ToggleChip } from '../components/ui/Chip'
import { cx } from '../components/ui/cx'
import { Disclosure } from '../components/ui/Disclosure'
import { Input, Select } from '../components/ui/Field'
import { Menu } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Pagination } from '../components/ui/Pagination'
import { Panel } from '../components/ui/Panel'
import { SearchInput } from '../components/ui/SearchInput'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { Tabs } from '../components/ui/Tabs'
import { countOf, formatDateTime, shortAgo } from '../lib/format'

interface Filters {
  site: string
  onlyNew: boolean
  onlyMatch: boolean
  includeGone: boolean
  includeHidden: boolean
  q: string
}

const DEFAULT_FILTERS: Filters = { site: '', onlyNew: false, onlyMatch: true, includeGone: false, includeHidden: false, q: '' }

/** Career sites of chosen companies: one feed of their matching vacancies, and the sites themselves on a tab of their own. */
export default function SitesPage() {
  const qc = useQueryClient()
  const sites = useQuery({ queryKey: ['sites'], queryFn: () => apiGet<SitesView>('/api/sites'), refetchInterval: 30000 })
  const [tab, setTab] = useState<'feed' | 'sites'>('feed')
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS)
  const [page, setPage] = useState(1)
  const params = new URLSearchParams({
    only_new: String(filters.onlyNew),
    only_match: String(filters.onlyMatch),
    include_gone: String(filters.includeGone),
    include_hidden: String(filters.includeHidden),
    page: String(page),
    per_page: '50',
  })
  if (filters.site) params.set('site', filters.site)
  if (filters.q.trim()) params.set('q', filters.q.trim())
  const feed = useQuery({
    queryKey: ['site-vacancies', filters, page],
    queryFn: () => apiGet<SiteVacancyList>(`/api/sites/vacancies?${params.toString()}`),
    refetchInterval: 30000,
  })
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['sites'] })
    void qc.invalidateQueries({ queryKey: ['site-vacancies'] })
    void qc.invalidateQueries({ queryKey: ['sites-summary'] })
  }
  const syncAll = useMutation({ mutationFn: () => apiPost<{ items: CompanySite[] }>('/api/sites/sync'), onSuccess: invalidate })
  const markAllSeen = useMutation({
    mutationFn: () => apiPost<{ marked: number }>('/api/sites/vacancies/mark-seen', filters.site ? { site_key: filters.site } : {}),
    onSuccess: invalidate,
  })
  const items = sites.data?.items ?? []
  const newTotal = feed.data?.new_count ?? 0
  const running = Boolean(sites.data?.running) || syncAll.isPending
  const setFilter = (patch: Partial<Filters>) => {
    setFilters((f) => ({ ...f, ...patch }))
    setPage(1)
  }

  const meta: string[] = []
  if (sites.data) {
    meta.push(countOf(items.length, ['сайт', 'сайта', 'сайтов']))
    if (newTotal > 0) meta.push(`${newTotal} ${newTotal === 1 ? 'новая вакансия' : 'новых вакансий'}`)
    meta.push(sites.data.interval_min > 0 ? `проверка каждые ${sites.data.interval_min} мин` : 'автоматическая проверка выключена (SITES_SYNC_MIN=0)')
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Сайты компаний"
        meta={meta.join(' · ')}
        actions={
          <>
            <Button variant="ghost" onClick={() => markAllSeen.mutate()} disabled={markAllSeen.isPending || newTotal === 0}>
              Отметить всё просмотренным
            </Button>
            <Button onClick={() => syncAll.mutate()} disabled={running}>
              <RefreshCw aria-hidden className={cx(running && 'animate-spin motion-reduce:animate-none')} />
              {running ? 'Проверяю…' : 'Проверить сайты'}
            </Button>
          </>
        }
      />
      {(syncAll.error || markAllSeen.error) && <ErrorNotice error={syncAll.error ?? markAllSeen.error} title="Действие не выполнено" />}

      <Tabs
        label="Разделы"
        value={tab}
        onChange={(t) => setTab(t as 'feed' | 'sites')}
        items={[
          { id: 'feed', label: 'Вакансии', count: newTotal || undefined, attention: true },
          { id: 'sites', label: 'Сайты', count: items.length || undefined },
        ]}
      />

      {tab === 'feed' ? (
        <section aria-label="Вакансии с сайтов" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <SearchInput label="Поиск по названию и городу" placeholder="Название или город" value={filters.q} onChange={(q) => setFilter({ q })} />
            <Select aria-label="Сайт" wrapperClassName="w-full sm:w-60" value={filters.site} onChange={(e) => setFilter({ site: e.target.value })}>
              <option value="">Все сайты</option>
              {items.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.name}
                  {s.new_matches ? ` (новых ${s.new_matches})` : ''}
                </option>
              ))}
            </Select>
            <ToggleChip pressed={filters.onlyNew} onToggle={() => setFilter({ onlyNew: !filters.onlyNew })}>
              Только новые
            </ToggleChip>
            <ToggleChip pressed={filters.onlyMatch} onToggle={() => setFilter({ onlyMatch: !filters.onlyMatch })}>
              Только подходящие
            </ToggleChip>
            <ToggleChip pressed={filters.includeGone} onToggle={() => setFilter({ includeGone: !filters.includeGone })}>
              Со снятыми
            </ToggleChip>
            <ToggleChip pressed={filters.includeHidden} onToggle={() => setFilter({ includeHidden: !filters.includeHidden })}>
              Со скрытыми
            </ToggleChip>
            {feed.data && <span className="ml-auto text-caption tabular-nums text-ink-3">{countOf(feed.data.total, ['вакансия', 'вакансии', 'вакансий'])}</span>}
          </div>

          {feed.isLoading && (
            <div aria-busy="true" aria-label="Загрузка вакансий" className="space-y-3">
              {Array.from({ length: 3 }, (_, i) => (
                <div key={i} className="space-y-2 rounded-lg border border-line bg-surface p-4">
                  <Skeleton className="h-3 w-48" />
                  <Skeleton className="h-4 w-1/2" />
                </div>
              ))}
            </div>
          )}
          {feed.error && <ErrorNotice error={feed.error} title="Вакансии не загрузились" onRetry={() => void feed.refetch()} />}
          {feed.data && feed.data.items.length === 0 && (
            <div className="rounded-lg border border-line bg-surface">
              <EmptyState title={filters.onlyNew ? 'Новых вакансий нет' : 'Вакансий по этим условиям нет'}>{filters.onlyNew ? undefined : 'Нажмите «Проверить сайты», чтобы прочитать их заново.'}</EmptyState>
            </div>
          )}
          {feed.data && feed.data.items.length > 0 && (
            <div className="space-y-2.5">
              {feed.data.items.map((v) => (
                <VacancyCard key={v.id} vacancy={v} onChanged={invalidate} />
              ))}
            </div>
          )}
          {feed.data && <Pagination className="rounded-lg border bg-surface" page={page - 1} perPage={feed.data.per_page} total={feed.data.total} onPage={(p) => setPage(p + 1)} />}
        </section>
      ) : (
        <Panel title="Сайты" flush meta={sites.data && (sites.data.interval_min > 0 ? `Панель проверяет сайты каждые ${sites.data.interval_min} мин.` : 'Автоматическая проверка выключена (SITES_SYNC_MIN=0).')}>
          <div className="px-4 pb-4 sm:px-5">
            <Disclosure summary="Как это работает">
              <ul className="list-disc space-y-1 pl-5 text-caption text-ink-2">
                <li>Панель читает списки вакансий на сайтах компаний так же, как их читает браузер: без входа и без обхода защиты.</li>
                <li>
                  Подходящей считается вакансия в городе из SITES_CITIES ({(sites.data?.cities ?? []).join(', ') || '—'}; офис или гибрид) или удалённая из любого города; из заголовков исключаются
                  стоп-слова анкеты; у сайта можно задать ключевые слова.
                </li>
                <li>Новые подходящие вакансии приходят в Telegram-бота (событие «Вакансия на сайте компании»). Первое чтение сайта загружает текущий список без уведомлений.</li>
                <li>Новый сайт добавляется адаптером в коде: просто назовите его.</li>
              </ul>
            </Disclosure>
            {sites.data && Object.keys(sites.data.unsupported).length > 0 && (
              <Notice tone="warn" className="mt-3" title="Эти сайты прочитать нельзя">
                <ul className="space-y-0.5">
                  {Object.entries(sites.data.unsupported).map(([host, why]) => (
                    <li key={host}>
                      <span className="font-medium text-ink">{host}</span>: {why}
                    </li>
                  ))}
                </ul>
              </Notice>
            )}
          </div>
          {sites.isLoading && (
            <div aria-busy="true" aria-label="Загрузка сайтов" className="space-y-3 px-5 pb-5">
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-1/3" />
            </div>
          )}
          {sites.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={sites.error} title="Сайты не загрузились" onRetry={() => void sites.refetch()} />}
          {items.length > 0 && (
            <Table label="Сайты" minWidth={820}>
              <thead>
                <tr>
                  <Th>Сайт</Th>
                  <Th>Ключевые слова</Th>
                  <Th>В бота</Th>
                  <Th align="right">Вакансий</Th>
                  <Th>Проверка</Th>
                  <Th>
                    <span className="sr-only">Действия</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {items.map((s) => (
                  <SiteRow
                    key={s.key}
                    site={s}
                    onChanged={invalidate}
                    onShowVacancies={() => {
                      setFilter({ site: s.key })
                      setTab('feed')
                    }}
                  />
                ))}
              </tbody>
            </Table>
          )}
        </Panel>
      )}
    </div>
  )
}

function SiteRow({ site, onChanged, onShowVacancies }: { site: CompanySite; onChanged: () => void; onShowVacancies: () => void }) {
  const [keywords, setKeywords] = useState(site.keywords ?? '')
  const update = useMutation({
    mutationFn: (patch: Partial<Pick<CompanySite, 'enabled' | 'notify' | 'keywords'>>) => apiPut<CompanySite>(`/api/sites/${site.key}`, patch),
    onSuccess: onChanged,
  })
  const sync = useMutation({ mutationFn: () => apiPost<CompanySite>(`/api/sites/${site.key}/sync`), onSuccess: onChanged })
  const busy = update.isPending || sync.isPending
  const error = update.error ?? sync.error
  return (
    <tr className={cx(!site.enabled && 'text-ink-3')}>
      <Td className="min-w-[14rem]">
        <div className="font-medium text-ink">
          {site.name}
          {!site.enabled && (
            <Badge tone="outline" className="ml-2 align-[1px]">
              отключён
            </Badge>
          )}
        </div>
        <a className="text-caption text-accent underline-offset-2 hover:underline" href={site.url} target="_blank" rel="noreferrer">
          {site.url.replace('https://', '')}
        </a>
        {site.note && <div className="text-caption text-ink-3">{site.note}</div>}
        {error && <div className="mt-1 text-caption text-bad">{errorMessage(error)}</div>}
      </Td>
      <Td>
        <Input
          aria-label={`Ключевые слова для ${site.name}`}
          className="w-44"
          value={keywords}
          placeholder="Любые"
          disabled={busy}
          onChange={(e) => setKeywords(e.target.value)}
          onBlur={() => {
            if ((keywords.trim() || null) !== (site.keywords ?? null)) update.mutate({ keywords: keywords.trim() })
          }}
        />
      </Td>
      <Td>
        <input
          type="checkbox"
          className="mt-2 size-4 cursor-pointer accent-accent"
          aria-label={`Присылать вакансии ${site.name} в бота`}
          checked={site.notify}
          disabled={busy || !site.enabled}
          onChange={(e) => update.mutate({ notify: e.target.checked })}
        />
      </Td>
      <Td align="right" className="whitespace-nowrap">
        <button type="button" className="rounded font-medium text-accent underline-offset-2 hover:underline" onClick={onShowVacancies} title="Показать вакансии сайта">
          {site.vacancies_count} / {site.matches}
        </button>
        <div className="text-xs text-ink-3">всего / подходят</div>
        {site.new_matches > 0 && <div className="text-xs text-accent-strong">новых {site.new_matches}</div>}
      </Td>
      <Td className="whitespace-nowrap text-ink-2">
        {!site.enabled ? (
          <span className="text-ink-3">не проверяется</span>
        ) : site.last_checked_at ? (
          <span className="tabular-nums" title={formatDateTime(site.last_checked_at)}>
            {shortAgo(site.last_checked_at)}
          </span>
        ) : (
          '—'
        )}
        {site.enabled && site.last_new ? <span className="text-ink-3"> · новых {site.last_new}</span> : null}
        {site.last_error && <div className="max-w-xs whitespace-normal text-caption text-bad">{site.last_error}</div>}
      </Td>
      <Td className="w-px whitespace-nowrap">
        <div className="flex items-center justify-end gap-1">
          {site.enabled && (
            <Button size="sm" onClick={() => sync.mutate()} disabled={busy}>
              {sync.isPending ? 'Читаю…' : 'Проверить'}
            </Button>
          )}
          <Menu
            label={`Действия с сайтом ${site.name}`}
            items={[{ key: 'toggle', label: site.enabled ? 'Отключить' : 'Включить', disabled: busy, onSelect: () => update.mutate({ enabled: !site.enabled }) }]}
            trigger={(p) => (
              <Button {...p} variant="ghost" size="sm" iconOnly aria-label={`Действия с сайтом ${site.name}`} title="Действия">
                <MoreHorizontal aria-hidden />
              </Button>
            )}
          />
        </div>
      </Td>
    </tr>
  )
}

function VacancyCard({ vacancy: v, onChanged }: { vacancy: SiteVacancy; onChanged: () => void }) {
  const act = useMutation({
    mutationFn: (action: 'seen' | 'hide' | 'restore') => apiPost<SiteVacancy>(`/api/sites/vacancies/${v.id}/${action}`),
    onSuccess: onChanged,
  })
  const isNew = v.status === 'new'
  const faded = v.status === 'hidden' || v.status === 'gone'
  return (
    <article className={cx('rounded-lg border bg-surface px-4 py-3 sm:px-5', isNew ? 'border-accent/30' : 'border-line', faded && 'opacity-60')}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-ink-3">
        {isNew && (
          <span className="size-1.5 rounded-full bg-accent" title="Новая вакансия">
            <span className="sr-only">Новая.</span>
          </span>
        )}
        <span className="font-medium text-ink-2">{v.company ?? v.site}</span>
        {v.remote && <Badge tone="good">удалённо</Badge>}
        {v.work_format && !v.remote && <span>{v.work_format}</span>}
        {v.location && <span>{v.location.length > 80 ? `${v.location.slice(0, 80)}…` : v.location}</span>}
        {v.published_at && <span title={formatDateTime(v.published_at)}>опубликована {shortAgo(v.published_at)}</span>}
        <span title={formatDateTime(v.first_seen_at)}>замечена {shortAgo(v.first_seen_at)}</span>
        {v.status === 'gone' && <Badge tone="outline">снята с сайта</Badge>}
        {v.status === 'hidden' && <Badge tone="outline">скрыта</Badge>}
        {!v.is_match && <Badge>не по условиям</Badge>}
      </div>
      <h3 className="mt-1 text-sm font-semibold text-ink">
        <a href={v.url} target="_blank" rel="noreferrer" className="underline-offset-2 hover:underline">
          {v.title}
        </a>
      </h3>
      {v.snippet && <p className="mt-1 max-w-[72ch] text-sm text-ink-2">{v.snippet}</p>}
      <div className="-ml-2.5 mt-1.5 flex flex-wrap items-center gap-1">
        <a className="inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-caption font-medium text-accent hover:bg-sunken" href={v.url} target="_blank" rel="noreferrer">
          Открыть на сайте
          <ExternalLink aria-hidden className="size-3.5" />
        </a>
        {isNew && (
          <Button variant="ghost" size="sm" onClick={() => act.mutate('seen')} disabled={act.isPending}>
            Просмотрено
          </Button>
        )}
        {v.status !== 'hidden' ? (
          <Button variant="ghost" size="sm" onClick={() => act.mutate('hide')} disabled={act.isPending}>
            Скрыть
          </Button>
        ) : (
          <Button variant="ghost" size="sm" onClick={() => act.mutate('restore')} disabled={act.isPending}>
            Вернуть
          </Button>
        )}
      </div>
      {act.error && <p className="mt-1 text-caption text-bad">{errorMessage(act.error)}</p>}
    </article>
  )
}
