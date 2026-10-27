import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, MoreHorizontal, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { apiDelete, apiGet, apiPost, apiPut, errorMessage } from '../api/client'
import type { ChannelPost, ChannelPostList, ChannelsBulkResult, ChannelsView, TelegramChannel, ToPanelResult } from '../api/types'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { ToggleChip } from '../components/ui/Chip'
import { cx } from '../components/ui/cx'
import { Disclosure } from '../components/ui/Disclosure'
import { Checkbox, Field, Input, Select, Textarea } from '../components/ui/Field'
import { Menu } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Pagination } from '../components/ui/Pagination'
import { Panel } from '../components/ui/Panel'
import { SearchInput } from '../components/ui/SearchInput'
import { EmptyState, ErrorNotice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { Tabs } from '../components/ui/Tabs'
import { countOf, formatDateTime, shortAgo } from '../lib/format'

const MODE_LABELS: Record<TelegramChannel['mode'], string> = { vacancies: 'Только вакансии', all: 'Все посты' }

interface Filters {
  channelId: number
  onlyNew: boolean
  onlyVacancies: boolean
  includeHidden: boolean
  q: string
}

const DEFAULT_FILTERS: Filters = { channelId: 0, onlyNew: false, onlyVacancies: true, includeHidden: false, q: '' }

/** Public Telegram channels with vacancies: one feed of their posts, and the channels themselves on a tab of their own. */
export default function ChannelsPage() {
  const qc = useQueryClient()
  const channels = useQuery({ queryKey: ['channels'], queryFn: () => apiGet<ChannelsView>('/api/channels'), refetchInterval: 30000 })
  const [tab, setTab] = useState<'feed' | 'channels'>('feed')
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS)
  const [page, setPage] = useState(1)
  const params = new URLSearchParams({
    only_new: String(filters.onlyNew),
    only_vacancies: String(filters.onlyVacancies),
    include_hidden: String(filters.includeHidden),
    page: String(page),
    per_page: '50',
  })
  if (filters.channelId) params.set('channel_id', String(filters.channelId))
  if (filters.q.trim()) params.set('q', filters.q.trim())
  const posts = useQuery({
    queryKey: ['channel-posts', filters, page],
    queryFn: () => apiGet<ChannelPostList>(`/api/channels/posts?${params.toString()}`),
    refetchInterval: 30000,
  })
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['channels'] })
    void qc.invalidateQueries({ queryKey: ['channel-posts'] })
    void qc.invalidateQueries({ queryKey: ['channels-summary'] })
  }
  const syncAll = useMutation({ mutationFn: () => apiPost<{ items: TelegramChannel[] }>('/api/channels/sync'), onSuccess: invalidate })
  const markAllSeen = useMutation({
    mutationFn: () => apiPost<{ marked: number }>('/api/channels/posts/mark-seen', filters.channelId ? { channel_id: filters.channelId } : {}),
    onSuccess: invalidate,
  })
  const reflag = useMutation({ mutationFn: () => apiPost<{ posts: number; changed: number }>('/api/channels/reflag'), onSuccess: invalidate })

  const items = channels.data?.items ?? []
  const newTotal = posts.data?.new_count ?? 0
  const running = Boolean(channels.data?.running) || syncAll.isPending
  const setFilter = (patch: Partial<Filters>) => {
    setFilters((f) => ({ ...f, ...patch }))
    setPage(1)
  }

  const meta: string[] = []
  if (channels.data) {
    meta.push(countOf(items.length, ['канал', 'канала', 'каналов']))
    if (newTotal > 0) meta.push(`${newTotal} ${newTotal === 1 ? 'новый пост' : 'новых постов'}`)
    meta.push(channels.data.interval_min > 0 ? `проверка каждые ${channels.data.interval_min} мин` : 'автоматическая проверка выключена (CHANNEL_SYNC_MIN=0)')
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Каналы"
        meta={meta.join(' · ')}
        actions={
          <>
            <Button variant="ghost" onClick={() => markAllSeen.mutate()} disabled={markAllSeen.isPending || newTotal === 0}>
              Отметить всё просмотренным
            </Button>
            <Button onClick={() => syncAll.mutate()} disabled={running || items.length === 0}>
              <RefreshCw aria-hidden className={cx(running && 'animate-spin motion-reduce:animate-none')} />
              {running ? 'Проверяю…' : 'Проверить каналы'}
            </Button>
          </>
        }
      />
      {(syncAll.error || markAllSeen.error) && <ErrorNotice error={syncAll.error ?? markAllSeen.error} title="Действие не выполнено" />}

      <Tabs
        label="Разделы"
        value={tab}
        onChange={(t) => setTab(t as 'feed' | 'channels')}
        items={[
          { id: 'feed', label: 'Лента', count: newTotal || undefined, attention: true },
          { id: 'channels', label: 'Каналы', count: items.length || undefined },
        ]}
      />

      {tab === 'feed' ? (
        <section aria-label="Лента постов" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <SearchInput label="Поиск по тексту постов" placeholder="Поиск по тексту" value={filters.q} onChange={(q) => setFilter({ q })} />
            <Select aria-label="Канал" wrapperClassName="w-full sm:w-64" value={filters.channelId} onChange={(e) => setFilter({ channelId: Number(e.target.value) })}>
              <option value={0}>Все каналы</option>
              {items.map((c) => (
                <option key={c.id} value={c.id}>
                  @{c.username}
                  {c.new_posts ? ` (новых ${c.new_posts})` : ''}
                  {c.enabled ? '' : ' (отключён)'}
                </option>
              ))}
            </Select>
            <ToggleChip pressed={filters.onlyNew} onToggle={() => setFilter({ onlyNew: !filters.onlyNew })}>
              Только новые
            </ToggleChip>
            <ToggleChip pressed={filters.onlyVacancies} onToggle={() => setFilter({ onlyVacancies: !filters.onlyVacancies })}>
              Только вакансии
            </ToggleChip>
            <ToggleChip pressed={filters.includeHidden} onToggle={() => setFilter({ includeHidden: !filters.includeHidden })}>
              Со скрытыми
            </ToggleChip>
            {posts.data && <span className="ml-auto text-caption tabular-nums text-ink-3">{countOf(posts.data.total, ['пост', 'поста', 'постов'])}</span>}
          </div>

          {posts.isLoading && (
            <div aria-busy="true" aria-label="Загрузка ленты" className="space-y-3">
              {Array.from({ length: 3 }, (_, i) => (
                <div key={i} className="space-y-2 rounded-lg border border-line bg-surface p-4">
                  <Skeleton className="h-3 w-40" />
                  <Skeleton className="h-4 w-2/3" />
                  <Skeleton className="h-3 w-full" />
                </div>
              ))}
            </div>
          )}
          {posts.error && <ErrorNotice error={posts.error} title="Лента не загрузилась" onRetry={() => void posts.refetch()} />}
          {posts.data && posts.data.items.length === 0 && (
            <div className="rounded-lg border border-line bg-surface">
              <EmptyState
                title={items.length === 0 ? 'Каналов пока нет' : filters.onlyNew ? 'Новых постов нет' : 'Постов по этим условиям нет'}
                action={
                  items.length === 0 ? (
                    <Button variant="primary" onClick={() => setTab('channels')}>
                      Добавить канал
                    </Button>
                  ) : undefined
                }
              >
                {items.length === 0 ? 'Добавьте каналы: их посты появятся здесь, а новые вакансии придут в Telegram-бота.' : undefined}
              </EmptyState>
            </div>
          )}
          {posts.data && posts.data.items.length > 0 && (
            <div className="space-y-3">
              {posts.data.items.map((p) => (
                <PostCard key={p.id} post={p} onChanged={invalidate} />
              ))}
            </div>
          )}
          {posts.data && (
            <Pagination className="rounded-lg border bg-surface" page={page - 1} perPage={posts.data.per_page} total={posts.data.total} onPage={(p) => setPage(p + 1)} />
          )}
        </section>
      ) : (
        <div className="flex flex-col gap-5">
          <AddChannelForm onAdded={invalidate} languages={channels.data?.languages ?? []} />
          <Panel
            title="Каналы"
            meta={channels.data && (channels.data.interval_min > 0 ? `Панель проверяет каналы каждые ${channels.data.interval_min} мин.` : 'Автоматическая проверка выключена (CHANNEL_SYNC_MIN=0): только кнопкой.')}
            actions={
              items.length > 0 && (
                <>
                  {reflag.data && (
                    <span className="text-caption text-ink-3">
                      изменено {reflag.data.changed} из {reflag.data.posts}
                    </span>
                  )}
                  <Button variant="ghost" size="sm" onClick={() => reflag.mutate()} disabled={reflag.isPending} title="Заново определить, какие сохранённые посты похожи на вакансию">
                    {reflag.isPending ? 'Пересчитываю…' : 'Пересчитать признак вакансии'}
                  </Button>
                </>
              )
            }
            flush
          >
            {reflag.error && <ErrorNotice className="mx-4 mb-3 sm:mx-5" error={reflag.error} title="Пересчёт не выполнен" />}
            {channels.isLoading && (
              <div aria-busy="true" aria-label="Загрузка каналов" className="space-y-3 px-5 pb-5">
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-4 w-1/3" />
              </div>
            )}
            {channels.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={channels.error} title="Каналы не загрузились" onRetry={() => void channels.refetch()} />}
            {channels.data && items.length === 0 && <EmptyState inset="panel" title="Каналов пока нет">Добавьте первый в форме выше.</EmptyState>}
            {items.length > 0 && (
              <Table label="Каналы" minWidth={920}>
                <thead>
                  <tr>
                    <Th>Канал</Th>
                    <Th>Что собирать</Th>
                    <Th>Ключевые слова</Th>
                    <Th>В бота</Th>
                    <Th align="right">Постов</Th>
                    <Th>Проверка</Th>
                    <Th>
                      <span className="sr-only">Действия</span>
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((c) => (
                    <ChannelRow
                      key={c.id}
                      channel={c}
                      onChanged={invalidate}
                      onShowPosts={() => {
                        setFilter({ channelId: c.id })
                        setTab('feed')
                      }}
                    />
                  ))}
                </tbody>
              </Table>
            )}
          </Panel>
        </div>
      )}
    </div>
  )
}

function AddChannelForm({ onAdded, languages }: { onAdded: () => void; languages: string[] }) {
  const [input, setInput] = useState('')
  const [mode, setMode] = useState<TelegramChannel['mode']>('vacancies')
  const [keywords, setKeywords] = useState('')
  const [notify, setNotify] = useState(true)
  const add = useMutation({
    mutationFn: () => apiPost<ChannelsBulkResult>('/api/channels/bulk', { input: input.trim(), mode, keywords: keywords.trim() || null, notify }),
    onSuccess: (data) => {
      // keep only the entries that failed, so they can be fixed and resent
      setInput(data.errors.map((e) => e.input).join('\n'))
      if (data.added.length) {
        setKeywords('')
        onAdded()
      }
    },
  })
  return (
    <Panel
      title="Добавить канал"
      meta="Панель читает публичные каналы через их веб-версию (t.me/s/канал): без входа в Telegram и без бота в канале."
    >
      <Disclosure summary="Как это работает" className="mb-4">
        <ul className="list-disc space-y-1 pl-5 text-caption text-ink-2">
          <li>Приватные каналы и каналы с выключенной веб-версией прочитать нельзя.</li>
          <li>При добавлении загружаются последние посты канала: они попадают в ленту как уже просмотренные.</li>
          <li>Дальше каждый новый пост появляется в ленте, а похожие на вакансию ещё и приходят ссылкой в Telegram-бота (событие «Вакансия из Telegram-канала» в настройках уведомлений).</li>
          <li>Каналы могут быть любыми: фильтры поисков hh.ru на них не действуют.</li>
          <li>Можно вставить сразу несколько каналов, через пробел или по одному в строке. Ссылка на папку (t.me/addlist/…) не подойдёт: Telegram не раскрывает её состав вне приложения.</li>
          <li>
            Посты не на разрешённых языках (CHANNEL_LANGUAGES в .env, сейчас {languages.length ? languages.join(', ') : 'любые'}) вакансиями не считаются и в бота не уходят.
          </li>
        </ul>
      </Disclosure>
      <form
        className="grid gap-4 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]"
        onSubmit={(e) => {
          e.preventDefault()
          if (input.trim()) add.mutate()
        }}
      >
        <Field label="@канал или ссылка, можно несколько">
          <Textarea className="min-h-[5.5rem]" rows={3} value={input} onChange={(e) => setInput(e.target.value)} placeholder={'@python_jobs https://t.me/devops_jobs\n@remote_jobs'} />
        </Field>
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Что собирать">
              <Select value={mode} onChange={(e) => setMode(e.target.value as TelegramChannel['mode'])}>
                <option value="vacancies">Только похожие на вакансии</option>
                <option value="all">Все посты канала</option>
              </Select>
            </Field>
            <Field label="Ключевые слова" hint="Необязательно, через запятую">
              <Input value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="python, django, remote" />
            </Field>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Checkbox label="Присылать в бота" checked={notify} onChange={(e) => setNotify(e.target.checked)} />
            <Button variant="primary" type="submit" disabled={add.isPending || !input.trim()}>
              {add.isPending ? 'Читаю канал…' : 'Добавить'}
            </Button>
          </div>
        </div>
      </form>
      {add.error && <ErrorNotice className="mt-3" error={add.error} title="Каналы не добавлены" />}
      {add.data && add.data.added.length > 0 && (
        <p className="mt-3 text-caption text-good">Добавлено: {add.data.added.map((c) => `@${c.username}${c.title ? ` («${c.title}»)` : ''} — постов ${c.posts_count}`).join('; ')}.</p>
      )}
      {add.data && add.data.errors.length > 0 && (
        <ul className="mt-3 space-y-0.5 text-caption text-bad">
          {add.data.errors.map((e) => (
            <li key={e.input}>
              <span className="font-medium">{e.input}</span>: {e.error}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

function ChannelRow({ channel, onChanged, onShowPosts }: { channel: TelegramChannel; onChanged: () => void; onShowPosts: () => void }) {
  const [keywords, setKeywords] = useState(channel.keywords ?? '')
  const update = useMutation({
    mutationFn: (patch: Partial<Pick<TelegramChannel, 'enabled' | 'mode' | 'keywords' | 'notify'>>) => apiPut<TelegramChannel>(`/api/channels/${channel.id}`, patch),
    onSuccess: onChanged,
  })
  const sync = useMutation({ mutationFn: () => apiPost<TelegramChannel>(`/api/channels/${channel.id}/sync`), onSuccess: onChanged })
  const remove = useMutation({ mutationFn: () => apiDelete(`/api/channels/${channel.id}`), onSuccess: onChanged })
  const busy = update.isPending || sync.isPending || remove.isPending
  const error = update.error ?? sync.error ?? remove.error
  return (
    <tr className={cx(!channel.enabled && 'text-ink-3')}>
      <Td className="min-w-[13rem]">
        <div className="font-medium text-ink">
          {channel.title ?? channel.username}
          {!channel.enabled && (
            <Badge tone="outline" className="ml-2 align-[1px]">
              отключён
            </Badge>
          )}
        </div>
        <a className="text-caption text-accent underline-offset-2 hover:underline" href={channel.url} target="_blank" rel="noreferrer">
          @{channel.username}
        </a>
        {error && <div className="mt-1 text-caption text-bad">{errorMessage(error)}</div>}
      </Td>
      <Td>
        <Select aria-label={`Что собирать из @${channel.username}`} wrapperClassName="w-44" value={channel.mode} disabled={busy} onChange={(e) => update.mutate({ mode: e.target.value as TelegramChannel['mode'] })}>
          {(Object.keys(MODE_LABELS) as TelegramChannel['mode'][]).map((m) => (
            <option key={m} value={m}>
              {MODE_LABELS[m]}
            </option>
          ))}
        </Select>
      </Td>
      <Td>
        <Input
          aria-label={`Ключевые слова для @${channel.username}`}
          className="w-44"
          value={keywords}
          placeholder="Любые"
          disabled={busy}
          onChange={(e) => setKeywords(e.target.value)}
          onBlur={() => {
            if ((keywords.trim() || null) !== (channel.keywords ?? null)) update.mutate({ keywords: keywords.trim() })
          }}
        />
      </Td>
      <Td>
        <input
          type="checkbox"
          className="mt-2 size-4 cursor-pointer accent-accent"
          aria-label={`Присылать вакансии @${channel.username} в бота`}
          title="Присылать новые вакансии этого канала в Telegram-бота"
          checked={channel.notify}
          disabled={busy || !channel.enabled}
          onChange={(e) => update.mutate({ notify: e.target.checked })}
        />
      </Td>
      <Td align="right">
        <button type="button" className="rounded font-medium text-accent underline-offset-2 hover:underline" onClick={onShowPosts} title="Показать посты канала в ленте">
          {channel.posts_count}
        </button>
        {channel.new_posts > 0 && <div className="text-xs text-accent-strong">новых {channel.new_posts}</div>}
      </Td>
      <Td className="whitespace-nowrap text-ink-2">
        {!channel.enabled ? (
          <span className="text-ink-3">не проверяется</span>
        ) : channel.last_checked_at ? (
          <span className="tabular-nums" title={formatDateTime(channel.last_checked_at)}>
            {shortAgo(channel.last_checked_at)}
          </span>
        ) : (
          '—'
        )}
        {channel.enabled && channel.last_new ? <span className="text-ink-3"> · новых {channel.last_new}</span> : null}
        {channel.last_error && <div className="max-w-xs whitespace-normal text-caption text-bad">{channel.last_error}</div>}
      </Td>
      <Td className="w-px whitespace-nowrap">
        <div className="flex items-center justify-end gap-1">
          {channel.enabled && (
            <Button size="sm" onClick={() => sync.mutate()} disabled={busy}>
              {sync.isPending ? 'Читаю…' : 'Проверить'}
            </Button>
          )}
          <Menu
            label={`Действия с @${channel.username}`}
            items={[
              {
                key: 'toggle',
                label: channel.enabled ? 'Отключить' : 'Включить',
                hint: channel.enabled ? 'перестать проверять, посты останутся в ленте' : 'снова проверять каждые N минут',
                disabled: busy,
                onSelect: () => update.mutate({ enabled: !channel.enabled }),
              },
              'separator',
              {
                key: 'remove',
                label: 'Удалить вместе с постами',
                tone: 'danger',
                disabled: busy,
                onSelect: () => {
                  if (window.confirm(`Удалить канал @${channel.username} вместе с его постами?`)) remove.mutate()
                },
              },
            ]}
            trigger={(p) => (
              <Button {...p} variant="ghost" size="sm" iconOnly aria-label={`Действия с @${channel.username}`} title="Действия">
                <MoreHorizontal aria-hidden />
              </Button>
            )}
          />
        </div>
      </Td>
    </tr>
  )
}

function PostCard({ post, onChanged }: { post: ChannelPost; onChanged: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const act = useMutation({
    mutationFn: (action: 'seen' | 'hide' | 'restore') => apiPost<ChannelPost>(`/api/channels/posts/${post.id}/${action}`),
    onSuccess: onChanged,
  })
  const toPanel = useMutation({
    mutationFn: () => apiPost<ToPanelResult>(`/api/channels/posts/${post.id}/to-panel`, { create_packet: true }),
    onSuccess: onChanged,
  })
  const toggle = () => {
    const next = !expanded
    setExpanded(next)
    if (next && post.status === 'new') act.mutate('seen')
  }
  const isNew = post.status === 'new'
  const hidden = post.status === 'hidden'
  return (
    <article className={cx('rounded-lg border bg-surface p-4 sm:px-5', isNew ? 'border-accent/30' : 'border-line', hidden && 'opacity-60')}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-ink-3">
        {isNew && (
          <span className="size-1.5 rounded-full bg-accent" title="Новый пост">
            <span className="sr-only">Новый.</span>
          </span>
        )}
        <span className="font-medium text-ink-2">{post.channel ?? `@${post.username}`}</span>
        <span>@{post.username}</span>
        {post.posted_at && (
          <span className="tabular-nums" title={formatDateTime(post.posted_at)}>
            {shortAgo(post.posted_at)}
          </span>
        )}
        {post.notified_at && <span title={`Отправлено в Telegram ${formatDateTime(post.notified_at)}`}>· в боте</span>}
        {hidden && <Badge tone="outline">скрыт</Badge>}
        {post.lang && post.lang !== 'ru' && <Badge>{post.lang}</Badge>}
        {!post.is_vacancy && <Badge>не похоже на вакансию</Badge>}
      </div>
      <h3 className="mt-1 text-sm font-semibold text-ink">
        {post.url ? (
          <a href={post.url} target="_blank" rel="noreferrer" className="underline-offset-2 hover:underline">
            {post.title}
          </a>
        ) : (
          post.title
        )}
      </h3>
      {expanded ? (
        post.html ? (
          <div className="mt-1 max-w-[72ch] text-sm text-ink [&_a]:text-accent [&_a]:underline [&_p]:my-2" dangerouslySetInnerHTML={{ __html: post.html }} />
        ) : (
          <div className="mt-1 max-w-[72ch] whitespace-pre-wrap text-sm text-ink">{post.text}</div>
        )
      ) : (
        <div className="mt-1 line-clamp-3 max-w-[72ch] whitespace-pre-line text-sm text-ink-2">{post.text}</div>
      )}
      <div className="-ml-2.5 mt-2 flex flex-wrap items-center gap-x-1 gap-y-1">
        <Button variant="ghost" size="sm" aria-expanded={expanded} onClick={toggle}>
          {expanded ? 'Свернуть' : 'Развернуть'}
        </Button>
        {post.url && (
          <a className="inline-flex h-7 items-center gap-1 rounded-md px-2.5 text-caption font-medium text-ink-2 hover:bg-sunken hover:text-ink" href={post.url} target="_blank" rel="noreferrer">
            Telegram
            <ExternalLink aria-hidden className="size-3.5" />
          </a>
        )}
        {post.hh_links.map((u) => (
          <a key={u} className="inline-flex h-7 max-w-full items-center gap-1 truncate rounded-md px-2.5 text-caption font-medium text-accent hover:bg-sunken" href={u} target="_blank" rel="noreferrer">
            {u.replace('https://', '')}
          </a>
        ))}
        {post.vacancy_id ? (
          <Link className="inline-flex h-7 items-center rounded-md px-2.5 text-caption font-medium text-good hover:bg-sunken" to={`/vacancies?open=${post.vacancy_id}`}>
            В панели: {post.vacancy_status_label ?? post.vacancy_status}
          </Link>
        ) : post.hh_links.length > 0 ? (
          <Button size="sm" className="mx-1" onClick={() => toPanel.mutate()} disabled={toPanel.isPending} title="Оценка и письмо через Claude">
            {toPanel.isPending ? 'Добавляю…' : 'В панель'}
          </Button>
        ) : null}
        {isNew && (
          <Button variant="ghost" size="sm" onClick={() => act.mutate('seen')} disabled={act.isPending}>
            Просмотрено
          </Button>
        )}
        {!hidden ? (
          <Button variant="ghost" size="sm" onClick={() => act.mutate('hide')} disabled={act.isPending}>
            Скрыть
          </Button>
        ) : (
          <Button variant="ghost" size="sm" onClick={() => act.mutate('restore')} disabled={act.isPending}>
            Вернуть
          </Button>
        )}
      </div>
      {(act.error || toPanel.error) && <p className="mt-1 text-caption text-bad">{errorMessage(act.error ?? toPanel.error)}</p>}
      {toPanel.data && toPanel.data.errors.length > 0 && <p className="mt-1 text-caption text-bad">{toPanel.data.errors.map((e) => e.error).join('; ')}</p>}
      {toPanel.data && toPanel.data.added.length > 0 && (
        <p className="mt-1 text-caption text-good">Добавлено в панель{toPanel.data.batch_ids.length ? ', пакет для /hh-batch собран' : ''}.</p>
      )}
    </article>
  )
}
