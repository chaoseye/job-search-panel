import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDownUp, Copy, ExternalLink, RefreshCw, Settings2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { apiGet, apiPost, apiPut } from '../api/client'
import type { JobsView, TenderDetail, TenderGroup, TenderList, TenderRow, TenderSettings } from '../api/types'
import { Badge, type BadgeTone } from '../components/ui/Badge'
import { Button, buttonClass } from '../components/ui/Button'
import { ToggleChip } from '../components/ui/Chip'
import { cx } from '../components/ui/cx'
import { Field, Input, Select, Textarea } from '../components/ui/Field'
import { Menu } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Pagination } from '../components/ui/Pagination'
import { SearchInput } from '../components/ui/SearchInput'
import { Sheet, SheetCloseButton } from '../components/ui/Sheet'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Tabs } from '../components/ui/Tabs'
import { formatDateTime } from '../lib/format'

export const TENDER_GROUPS: [TenderGroup, string][] = [
  ['relevant', 'Подходящие'],
  ['open', 'Все открытые'],
  ['drafted', 'Отклик готов'],
  ['responded', 'Откликнулся'],
  ['closed', 'Пропущенные и скрытые'],
  ['all', 'Все'],
]

const SORTS: [string, string][] = [
  ['fit', 'По совпадению'],
  ['published', 'Сначала новые'],
  ['deadline', 'По сроку заявок'],
  ['budget', 'По бюджету'],
]

const STATUS_TONE: Record<string, BadgeTone> = {
  new: 'info',
  interesting: 'info',
  drafting: 'neutral',
  drafted: 'good',
  responded: 'good',
  replied: 'good',
  declined: 'bad',
  skipped: 'neutral',
  hidden: 'outline',
}

/** «2026-09-30» → «30.09»; the days left until the deadline for an open tender. */
export function deadlineText(deadline: string | null, open: boolean, today: Date = new Date()): string {
  if (!deadline) return '—'
  const [y, m, d] = deadline.split('-').map(Number)
  const short = `${String(d).padStart(2, '0')}.${String(m).padStart(2, '0')}`
  if (!open) return short
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  const days = Math.round((new Date(y, m - 1, d).getTime() - start.getTime()) / 86400000)
  if (days < 0) return short
  return `${short} · ${days === 0 ? 'сегодня' : days === 1 ? 'завтра' : `${days} дн.`}`
}

/** Tenders of workspace.ru: the watched list, answers written by Claude that the user posts on the site. */
export default function TendersPage() {
  const qc = useQueryClient()
  const [group, setGroup] = useState<TenderGroup>('relevant')
  const [section, setSection] = useState('')
  const [q, setQ] = useState('')
  const [sort, setSort] = useState('fit')
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<number[]>([])
  const [openId, setOpenId] = useState<number | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [notice, setNotice] = useState('')

  const params = new URLSearchParams({ group, sort, page: String(page), per_page: '50' })
  if (section) params.set('section', section)
  if (q.trim()) params.set('q', q.trim())
  const list = useQuery({
    queryKey: ['tenders', group, section, q, sort, page],
    queryFn: () => apiGet<TenderList>(`/api/tenders?${params.toString()}`),
    refetchInterval: 60000,
  })
  const settings = useQuery({ queryKey: ['tender-settings'], queryFn: () => apiGet<TenderSettings>('/api/tenders/settings') })
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => apiGet<JobsView>('/api/jobs'), refetchInterval: 5000 })
  const running = (jobs.data?.running ?? []).includes('ws_tenders_sync')
  const lastRun = jobs.data?.runs.find((r) => r.job === 'ws_tenders_sync')
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['tenders'] })
    void qc.invalidateQueries({ queryKey: ['tender'] })
    void qc.invalidateQueries({ queryKey: ['tenders-summary'] })
    void qc.invalidateQueries({ queryKey: ['jobs'] })
  }
  const sync = useMutation({ mutationFn: () => apiPost('/api/tenders/sync', {}), onSuccess: invalidate })
  const replies = useMutation({
    mutationFn: (ids: number[]) => apiPost<{ queued: number; skipped: string[]; batch_id: string | null }>('/api/tenders/replies', { ids }),
    onSuccess: (r) => {
      setSelected([])
      setNotice(
        `Откликов в очереди для Claude: ${r.queued}` + (r.batch_id ? ` — пакет ${r.batch_id}, запустите /hh-batch` : '') + (r.skipped.length ? `. Пропущено: ${r.skipped.join('; ')}` : ''),
      )
      invalidate()
    },
  })

  const items = list.data?.items ?? []
  const counts = list.data?.counts
  const sections = settings.data?.available_sections ?? []
  const toggle = (id: number) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  const allOnPage = items.length > 0 && items.every((t) => selected.includes(t.id))
  const busy = sync.isPending || running

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Тендеры"
        meta={
          <>
            Тендеры workspace.ru на digital-услуги: панель раз в час читает первую страницу списка и выбранных разделов и сравнивает новые тендеры с анкетой.
            {lastRun && (
              <div>
                Последняя проверка {formatDateTime(lastRun.started_at)} — {lastRun.status}
                {lastRun.error ? ` (${lastRun.error})` : ''}
              </div>
            )}
          </>
        }
        actions={
          <>
            <Button variant="ghost" onClick={() => setSettingsOpen(true)}>
              <Settings2 aria-hidden />
              Настройки
            </Button>
            <Button onClick={() => sync.mutate()} disabled={busy}>
              <RefreshCw aria-hidden className={cx(busy && 'animate-spin motion-reduce:animate-none')} />
              {busy ? 'Проверяю…' : 'Проверить сейчас'}
            </Button>
          </>
        }
      />
      {sync.error && <ErrorNotice error={sync.error} title="Проверка не запустилась" />}

      <section aria-label="Список тендеров">
        <Tabs
          label="Группы тендеров"
          value={group}
          onChange={(id) => {
            setGroup(id as TenderGroup)
            setPage(0)
            setSelected([])
          }}
          items={TENDER_GROUPS.map(([id, label]) => ({ id, label, count: counts?.[id], attention: id === 'relevant' }))}
        />
        <div className="flex flex-wrap items-center gap-2 py-3">
          <SearchInput
            label="Поиск по названию и описанию"
            placeholder="Название или описание"
            value={q}
            onChange={(v) => {
              setQ(v)
              setPage(0)
            }}
          />
          <Select
            aria-label="Раздел"
            wrapperClassName="w-full sm:w-60"
            value={section}
            onChange={(e) => {
              setSection(e.target.value)
              setPage(0)
            }}
          >
            <option value="">Все разделы</option>
            {sections.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </Select>
          <Menu
            label="Сортировка"
            align="start"
            items={SORTS.map(([id, label]) => ({ key: id, label, checked: sort === id, onSelect: () => setSort(id) }))}
            trigger={(p) => (
              <Button {...p} variant="ghost">
                <ArrowDownUp aria-hidden />
                {SORTS.find(([id]) => id === sort)?.[1]}
              </Button>
            )}
          />
          <Button variant="primary" className="sm:ml-auto" disabled={!selected.length || replies.isPending} onClick={() => replies.mutate(selected)}>
            Подготовить отклики ({selected.length})
          </Button>
        </div>
        {notice && (
          <Notice
            tone="good"
            role="status"
            className="mb-3"
            action={
              <Button variant="ghost" size="sm" onClick={() => setNotice('')}>
                Скрыть
              </Button>
            }
          >
            {notice}
          </Notice>
        )}
        {replies.error && <ErrorNotice className="mb-3" error={replies.error} title="Отклики не поставлены в очередь" />}

        <div className="overflow-clip rounded-lg border border-line bg-surface">
          <div className="hidden border-b border-line px-4 py-2 text-xs font-medium text-ink-3 md:grid md:grid-cols-[1.25rem_minmax(0,1fr)_8.5rem_8rem_5.5rem_9rem] md:items-center md:gap-x-4">
            <span>
              {items.length > 0 && (
                <input
                  type="checkbox"
                  className="size-4 cursor-pointer accent-accent"
                  aria-label="Выбрать все на странице"
                  checked={allOnPage}
                  onChange={() => setSelected(allOnPage ? [] : items.map((t) => t.id))}
                />
              )}
            </span>
            <span>Тендер</span>
            <span>Бюджет</span>
            <span>Заявки до</span>
            <span className="text-right">Совпадение</span>
            <span>Статус</span>
          </div>
          {list.isLoading && (
            <div aria-busy="true" aria-label="Загрузка тендеров" className="divide-y divide-rule">
              {Array.from({ length: 5 }, (_, i) => (
                <div key={i} className="space-y-2 px-4 py-3">
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-3 w-1/3" />
                </div>
              ))}
            </div>
          )}
          {list.error && <ErrorNotice className="m-4" error={list.error} title="Тендеры не загрузились" onRetry={() => void list.refetch()} />}
          {list.data && items.length === 0 && (
            <EmptyState
              title="Здесь пусто"
              action={
                group !== 'open' ? (
                  <Button
                    onClick={() => {
                      setGroup('open')
                      setPage(0)
                    }}
                  >
                    Показать все открытые
                  </Button>
                ) : undefined
              }
            >
              Нажмите «Проверить сейчас», чтобы прочитать workspace.ru заново.
            </EmptyState>
          )}
          {items.length > 0 && (
            <ul aria-label="Тендеры" className="divide-y divide-rule">
              {items.map((t) => (
                <TenderListRow key={t.id} tender={t} checked={selected.includes(t.id)} onToggle={() => toggle(t.id)} onOpen={() => setOpenId(t.id)} />
              ))}
            </ul>
          )}
          {list.data && <Pagination page={page} perPage={list.data.per_page} total={list.data.total} onPage={setPage} />}
        </div>
      </section>

      {openId !== null && <TenderDrawer id={openId} onClose={() => setOpenId(null)} onChanged={invalidate} />}
      {settingsOpen && <TenderSettingsSheet data={settings.data} onClose={() => setSettingsOpen(false)} onSaved={invalidate} />}
    </div>
  )
}

function TenderListRow({ tender: t, checked, onToggle, onOpen }: { tender: TenderRow; checked: boolean; onToggle: () => void; onOpen: () => void }) {
  const meta = [t.section ?? t.service, t.city ? `заказчик из ${t.city}` : null, t.participants ? `откликов: ${t.participants}` : null, t.open ? null : 'приём заявок закрыт']
    .filter(Boolean)
    .join(' · ')
  return (
    <li
      className={cx(
        'grid grid-cols-[1.25rem_minmax(0,1fr)] gap-x-3 px-4 py-3 transition-colors hover:bg-sunken md:grid-cols-[1.25rem_minmax(0,1fr)_8.5rem_8rem_5.5rem_9rem] md:items-center md:gap-x-4',
        !t.open && 'text-ink-3',
      )}
    >
      <input type="checkbox" className="mt-0.5 size-4 cursor-pointer accent-accent md:mt-0" aria-label={`Выбрать: ${t.title}`} checked={checked} onChange={onToggle} />
      <div className="min-w-0">
        <div className="flex items-start gap-2">
          {t.status === 'new' && (
            <span className="mt-2 size-1.5 shrink-0 rounded-full bg-accent" title="Новый">
              <span className="sr-only">Новый.</span>
            </span>
          )}
          <button type="button" onClick={onOpen} className={cx('min-w-0 rounded-sm text-left text-sm font-medium underline-offset-2 hover:underline', t.open ? 'text-ink' : 'text-ink-2')}>
            {t.title}
          </button>
        </div>
        <div className="mt-0.5 text-caption text-ink-2">{meta}</div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-ink-2 md:hidden">
          <span className="font-medium text-ink">{t.budget ?? 'бюджет не указан'}</span>
          <span className="text-ink-3">·</span>
          <span>до {deadlineText(t.deadline_on, t.open)}</span>
          {t.fit_score !== null && (
            <>
              <span className="text-ink-3">·</span>
              <span>совпадение {t.fit_score}</span>
            </>
          )}
          <Badge tone={STATUS_TONE[t.status] ?? 'neutral'}>{t.status_label}</Badge>
        </div>
      </div>
      <span className="hidden text-sm tabular-nums text-ink md:block">{t.budget ?? '—'}</span>
      <span className="hidden whitespace-nowrap text-caption tabular-nums text-ink-2 md:block">{deadlineText(t.deadline_on, t.open)}</span>
      <span className="hidden text-right text-sm tabular-nums text-ink md:block" title={t.fit_notes.join('\n') || undefined}>
        {t.fit_score ?? '—'}
      </span>
      <span className="hidden md:block">
        <Badge tone={STATUS_TONE[t.status] ?? 'neutral'}>{t.status_label}</Badge>
      </span>
    </li>
  )
}

function TenderSettingsSheet({ data, onClose, onSaved }: { data: TenderSettings | undefined; onClose: () => void; onSaved: () => void }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<TenderSettings | null>(data ?? null)
  useEffect(() => {
    if (data && draft === null) setDraft(data)
  }, [data, draft])
  const save = useMutation({
    mutationFn: (body: TenderSettings) => apiPut<TenderSettings>('/api/tenders/settings', { sections: body.sections, min_score: body.min_score, rates: body.rates }),
    onSuccess: (saved) => {
      setDraft(saved)
      qc.setQueryData(['tender-settings'], saved)
      onSaved()
    },
  })
  const toggleSection = (key: string) => draft && setDraft({ ...draft, sections: draft.sections.includes(key) ? draft.sections.filter((s) => s !== key) : [...draft.sections, key] })
  return (
    <Sheet size="md" labelledBy="tender-settings-title" onClose={onClose}>
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-line pl-5 pr-3">
        <h2 id="tender-settings-title" className="text-[15px] font-semibold">
          Настройки монитора и откликов
        </h2>
        <SheetCloseButton />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        {!draft ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <form
            className="space-y-6"
            onSubmit={(e) => {
              e.preventDefault()
              save.mutate(draft)
            }}
          >
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium text-ink">Разделы, за которыми следить</legend>
              <p className="text-caption text-ink-3">Тендеры этих разделов подходят сразу, остальные проверяются по совпадению с анкетой.</p>
              <div className="flex flex-wrap gap-1.5 pt-1">
                {draft.available_sections.map((s) => (
                  <ToggleChip key={s.key} pressed={draft.sections.includes(s.key)} onToggle={() => toggleSection(s.key)}>
                    {s.label}
                  </ToggleChip>
                ))}
              </div>
            </fieldset>
            <Field label="Тендер из другого раздела подходит от совпадения" hint="От 0 до 100." className="max-w-xs">
              <Input
                type="number"
                min={0}
                max={100}
                value={draft.min_score}
                onChange={(e) => setDraft({ ...draft, min_score: Math.max(0, Math.min(100, Number(e.target.value) || 0)) })}
                aria-label="Порог совпадения"
              />
            </Field>
            <Field label="Форматы, ставки и сроки" hint="Claude берёт цену и срок отклика только отсюда, иначе оставит «уточнить».">
              <Textarea
                className="min-h-32"
                value={draft.rates}
                onChange={(e) => setDraft({ ...draft, rates: e.target.value })}
                placeholder={'Сайт-визитка — от 40 000 ₽, 1–2 недели; доработка сайта — 2 000 ₽ в час…'}
              />
            </Field>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="submit" variant="primary" disabled={save.isPending}>
                {save.isPending ? 'Сохраняю…' : 'Сохранить'}
              </Button>
              {save.isSuccess && (
                <span role="status" className="text-caption text-good">
                  Сохранено
                </span>
              )}
            </div>
            {save.error && <ErrorNotice error={save.error} title="Настройки не сохранены" />}
          </form>
        )}
      </div>
    </Sheet>
  )
}

export function TenderDrawer({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const qc = useQueryClient()
  const detail = useQuery({ queryKey: ['tender', id], queryFn: () => apiGet<TenderDetail>(`/api/tenders/${id}`) })
  const d = detail.data
  const [notes, setNotes] = useState('')
  const [copied, setCopied] = useState(false)
  // what the user typed, kept with the id of the reply version it belongs to: a new version shows its own text, a
  // refetch of the same one keeps the typing; the editor never renders empty before an effect fills it
  const replyId = d?.reply?.id ?? null
  const [edit, setEdit] = useState<{ id: number | null; text: string } | null>(null)
  const text = edit && edit.id === replyId ? edit.text : (d?.reply?.text ?? '')
  const setText = (value: string) => setEdit({ id: replyId, text: value })
  const refresh = (data?: TenderDetail) => {
    if (data) qc.setQueryData(['tender', id], data)
    onChanged()
  }
  const act = useMutation({
    mutationFn: ({ path, body, method }: { path: string; body?: unknown; method?: 'put' }) =>
      method === 'put' ? apiPut<TenderDetail>(`/api/tenders/${id}${path}`, body) : apiPost<TenderDetail>(`/api/tenders/${id}${path}`, body),
    onSuccess: (data) => refresh(data),
  })
  const draft = useMutation({
    mutationFn: () => apiPost<{ queued: number; skipped: string[]; batch_id: string | null }>('/api/tenders/replies', { ids: [id], notes: notes || null }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['tender', id] })
      onChanged()
    },
  })
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
    } catch {
      /* the text stays selectable in the editor */
    }
  }
  const error = act.error ?? draft.error

  return (
    <Sheet onClose={onClose} labelledBy="tender-title">
      <div className="flex h-12 shrink-0 items-center gap-1 border-b border-line px-3 sm:px-4">
        <span className="text-caption text-ink-3">Тендер</span>
        <div className="ml-auto flex items-center gap-1">
          {d && (
            <a className={buttonClass('ghost', 'sm')} href={d.url} target="_blank" rel="noreferrer">
              workspace.ru
              <ExternalLink aria-hidden />
            </a>
          )}
          <SheetCloseButton />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {detail.isLoading && (
          <div className="space-y-3 px-6 py-5">
            <Skeleton className="h-6 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="mt-6 h-40 w-full" />
          </div>
        )}
        {detail.error && <ErrorNotice className="m-5" error={detail.error} title="Не удалось открыть тендер" onRetry={() => void detail.refetch()} />}
        {d && (
          <div className="space-y-6 px-4 pb-10 pt-5 sm:px-6">
            <header className="space-y-3">
              <div>
                <h2 id="tender-title" className="text-lg font-semibold leading-snug text-ink">
                  {d.title}
                </h2>
                <div className="mt-1 text-sm text-ink-2">
                  {[d.budget, d.open ? `заявки до ${deadlineText(d.deadline_on, true)}` : 'приём заявок закрыт', d.city ? `заказчик из ${d.city}` : null, d.organizer].filter(Boolean).join(' · ')}
                </div>
                <div className="mt-0.5 text-caption text-ink-3">
                  {[
                    d.section,
                    d.published_on ? `опубликован ${deadlineText(d.published_on, false)}` : null,
                    d.participants != null ? `откликов: ${d.participants}` : null,
                    d.views != null ? `просмотров: ${d.views}` : null,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={STATUS_TONE[d.status] ?? 'neutral'}>{d.status_label}</Badge>
                {d.status === 'new' && (
                  <Button size="sm" onClick={() => act.mutate({ path: '/interesting' })} disabled={act.isPending}>
                    Интересно
                  </Button>
                )}
                {!['responded', 'replied', 'declined'].includes(d.status) && (
                  <Button size="sm" onClick={() => act.mutate({ path: '/responded' })} disabled={act.isPending}>
                    Я откликнулся
                  </Button>
                )}
                {/* the organizer's answer comes to the user from the site: recorded here by hand, a mistake mended */}
                {['responded', 'declined'].includes(d.status) && (
                  <Button size="sm" onClick={() => act.mutate({ path: '/replied' })} disabled={act.isPending}>
                    Ответили
                  </Button>
                )}
                {['responded', 'replied'].includes(d.status) && (
                  <Button size="sm" onClick={() => act.mutate({ path: '/declined' })} disabled={act.isPending}>
                    Отказ
                  </Button>
                )}
                {!['skipped', 'hidden'].includes(d.status) ? (
                  <>
                    <Button size="sm" variant="ghost" onClick={() => act.mutate({ path: '/skip' })} disabled={act.isPending}>
                      Пропустить
                    </Button>
                    <Button size="sm" variant="danger" onClick={() => act.mutate({ path: '/hide' })} disabled={act.isPending}>
                      Скрыть
                    </Button>
                  </>
                ) : (
                  <Button size="sm" variant="primary" onClick={() => act.mutate({ path: '/restore' })} disabled={act.isPending}>
                    Вернуть
                  </Button>
                )}
              </div>
              {d.responded_at && (
                <p className="text-caption text-ink-2">
                  Откликнулся {formatDateTime(d.responded_at)}
                  {/* after «Вернуть» the date stays until the answer is marked again: shown with the answer only */}
                  {d.answered_at && ['replied', 'declined'].includes(d.status) ? `, ${d.status === 'declined' ? 'отказ' : 'ответ'} ${formatDateTime(d.answered_at)}` : ''}.
                </p>
              )}
              {error && <ErrorNotice error={error} title="Действие не выполнено" />}
            </header>

            {(d.fit_notes.length > 0 || d.conditions.length > 0) && (
              <section className="space-y-2 text-sm">
                {d.fit_notes.length > 0 && (
                  <p className="text-ink-2">
                    <span className="font-medium text-ink">Совпадение {d.fit_score}:</span> {d.fit_notes.join(' · ')}
                  </p>
                )}
                {d.conditions.length > 0 && (
                  <dl className="grid grid-cols-[minmax(0,12rem)_minmax(0,1fr)] gap-x-4 gap-y-1 text-caption">
                    {d.conditions.map((c) => (
                      <div key={c.title} className="contents">
                        <dt className="text-ink-3">{c.title}</dt>
                        <dd className="text-ink">{c.value}</dd>
                      </div>
                    ))}
                  </dl>
                )}
              </section>
            )}

            <section>
              <h3 className="mb-2 text-sm font-semibold">Описание</h3>
              {d.description ? (
                <div className="max-w-[72ch] whitespace-pre-line text-prose text-ink">{d.description}</div>
              ) : (
                <p className="text-caption text-ink-2">{d.detail_error ?? 'Карточку тендера ещё не прочитали: откроется при следующей проверке.'}</p>
              )}
            </section>

            <section className="space-y-3 border-t border-line pt-5">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold">Отклик</h3>
                {d.request && ['queued', 'sent_in_batch'].includes(d.request.status) && (
                  <Badge tone="info">{d.request.batch_id ? `в пакете ${d.request.batch_id} — запустите /hh-batch` : 'ждёт следующего пакета'}</Badge>
                )}
                {d.request?.status === 'cancelled' && d.request.error && <span className="text-caption text-warn">Claude: {d.request.error}</span>}
              </div>
              {d.reply ? (
                <>
                  {d.reply.needs_clarification.length > 0 && (
                    <Notice tone="warn" title="Уточните перед отправкой">
                      <ul className="list-disc pl-4">
                        {d.reply.needs_clarification.map((c) => (
                          <li key={c}>{c}</li>
                        ))}
                      </ul>
                    </Notice>
                  )}
                  {d.reply.warnings.length > 0 && (
                    <Notice tone="warn" title="Замечания к отклику">
                      {d.reply.warnings.join('; ')}
                    </Notice>
                  )}
                  <Textarea className="min-h-72 text-prose" value={text} onChange={(e) => setText(e.target.value)} aria-label="Текст отклика" />
                  <div className="flex flex-wrap gap-2">
                    <Button variant={text !== d.reply.text ? 'primary' : 'secondary'} onClick={() => act.mutate({ path: '/reply', method: 'put', body: { text } })} disabled={text === d.reply.text || act.isPending}>
                      Сохранить правку
                    </Button>
                    <Button variant="ghost" onClick={() => void copy(text)}>
                      <Copy aria-hidden />
                      {copied ? 'Скопировано' : 'Скопировать отклик'}
                    </Button>
                    <a className={buttonClass('ghost')} href={d.url} target="_blank" rel="noreferrer">
                      Откликнуться на workspace.ru
                      <ExternalLink aria-hidden />
                    </a>
                  </div>
                </>
              ) : (
                <p className="text-caption text-ink-2">Отклика ещё нет.</p>
              )}
              {['new', 'interesting', 'drafted'].includes(d.status) && d.open && (
                <form
                  className="flex flex-col gap-2 sm:flex-row"
                  onSubmit={(e) => {
                    e.preventDefault()
                    draft.mutate()
                  }}
                >
                  <Input aria-label="Пожелание к отклику" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Пожелание к отклику, необязательно" />
                  <Button type="submit" variant={d.reply ? 'secondary' : 'primary'} disabled={draft.isPending}>
                    {d.reply ? 'Переписать через Claude' : 'Подготовить отклик'}
                  </Button>
                </form>
              )}
            </section>

            <Field label="Заметки">
              <Textarea
                className="min-h-20"
                defaultValue={d.notes ?? ''}
                onBlur={(e) => e.target.value !== (d.notes ?? '') && act.mutate({ path: '', method: 'put', body: { notes: e.target.value } })}
              />
            </Field>
          </div>
        )}
      </div>
    </Sheet>
  )
}
