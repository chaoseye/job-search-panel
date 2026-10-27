import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDownUp, Copy, ExternalLink, ListPlus, Mail, Search, Settings2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiPost, apiPut } from '../api/client'
import type { JobsView, StudioDetail, StudioLetterKind, StudioLetterStyle, StudioList, StudioRow, StudioSettings } from '../api/types'
import TagInput from '../components/TagInput'
import { Badge, type BadgeTone } from '../components/ui/Badge'
import { Button, buttonClass } from '../components/ui/Button'
import { ToggleChip } from '../components/ui/Chip'
import { cx } from '../components/ui/cx'
import { Disclosure } from '../components/ui/Disclosure'
import { Checkbox, Field, Input, Select, Textarea } from '../components/ui/Field'
import { Menu } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Pagination } from '../components/ui/Pagination'
import { SearchInput } from '../components/ui/SearchInput'
import { Sheet, SheetCloseButton } from '../components/ui/Sheet'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Tabs } from '../components/ui/Tabs'
import { formatDateTime } from '../lib/format'

export const STUDIO_GROUPS: [string, string][] = [
  ['work', 'В работе'],
  ['ready', 'Есть контакты'],
  ['drafted', 'Письмо готово'],
  ['sent', 'Отправлено'],
  ['replied', 'Ответили / отказ'],
  ['closed', 'Пропущенные и скрытые'],
  ['all', 'Все'],
]
const SOURCES: [string, string][] = [
  ['', 'Все источники'],
  ['hh_search', 'hh.ru: поиск'],
  ['hh_vacancies', 'hh.ru: вакансии в базе'],
  ['list', 'Мой список'],
  ['channel', 'Telegram-канал'],
]
const SOURCE_SHORT: Record<string, string> = { hh_search: 'hh', hh_vacancies: 'hh·вакансии', list: 'список', channel: 'канал' }
const KIND_OPTIONS: [string, string][] = [
  ['', 'Любое направление'],
  ['dev', 'Разработка'],
  ['design', 'Дизайн'],
  ['mixed', 'Разработка и дизайн'],
]
const SORTS: [string, string][] = [
  ['fit', 'По совпадению'],
  ['recent', 'Сначала новые'],
  ['name', 'По названию'],
]
const LETTER_KIND_LABELS: Record<StudioLetterKind, string> = {
  application: 'инициативный отклик',
  offer: 'предложение услуг',
}
const STYLE_TABS: Record<StudioLetterStyle, string> = { business: 'Деловой', simple: 'Простой' }
const STATUS_TONE: Record<string, BadgeTone> = {
  new: 'neutral',
  ready: 'info',
  no_contacts: 'outline',
  drafting: 'neutral',
  drafted: 'good',
  sent: 'good',
  replied: 'good',
  declined: 'bad',
  skipped: 'neutral',
  hidden: 'outline',
}

/** mailto: with the letter ready to send from the user's own mail program (the panel never sends it itself). */
export function mailtoHref(to: string, subject: string, body: string): string {
  return `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`
}

const ROW_GRID = 'md:grid-cols-[1.25rem_minmax(0,1fr)_9rem_9rem_5.5rem_8rem_8.5rem]'

/** Development and design studios: finding them, their contacts, letters written by Claude that the user sends. */
export default function StudiosPage() {
  const qc = useQueryClient()
  const [group, setGroup] = useState('work')
  const [source, setSource] = useState('')
  const [kind, setKind] = useState('')
  const [q, setQ] = useState('')
  const [withContacts, setWithContacts] = useState(false)
  const [sort, setSort] = useState('fit')
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<number[]>([])
  const [openId, setOpenId] = useState<number | null>(null)
  const [sheet, setSheet] = useState<'settings' | 'import' | null>(null)
  const [notice, setNotice] = useState('')

  const params = new URLSearchParams({ group, sort, page: String(page), per_page: '50' })
  if (source) params.set('source', source)
  if (kind) params.set('kind', kind)
  if (q.trim()) params.set('q', q.trim())
  if (withContacts) params.set('with_contacts', 'true')
  const list = useQuery({
    queryKey: ['studios', group, source, kind, q, withContacts, sort, page],
    queryFn: () => apiGet<StudioList>(`/api/studios?${params.toString()}`),
    refetchInterval: 30000,
  })
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => apiGet<JobsView>('/api/jobs'), refetchInterval: 5000 })
  const running = jobs.data?.running ?? []
  const lastRun = (job: string) => jobs.data?.runs.find((r) => r.job === job)
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['studios'] })
    void qc.invalidateQueries({ queryKey: ['studio'] })
    void qc.invalidateQueries({ queryKey: ['studios-summary'] })
    void qc.invalidateQueries({ queryKey: ['jobs'] })
  }
  const find = useMutation({ mutationFn: () => apiPost('/api/studios/find', {}), onSuccess: invalidate })
  const contacts = useMutation({
    mutationFn: (ids: number[] | null) => apiPost('/api/studios/contacts', ids ? { ids } : {}),
    onSuccess: invalidate,
  })
  const letters = useMutation({
    mutationFn: (ids: number[]) => apiPost<{ queued: number; skipped: string[]; batch_id: string | null }>('/api/studios/letters', { ids }),
    onSuccess: (r) => {
      setSelected([])
      setNotice(`Писем в очереди для Claude: ${r.queued}` + (r.batch_id ? ` — пакет ${r.batch_id}, запустите /hh-batch` : '') + (r.skipped.length ? `. Пропущено: ${r.skipped.join('; ')}` : ''))
      invalidate()
    },
  })

  const items = list.data?.items ?? []
  const counts = list.data?.counts
  const countFor = (id: string): number | undefined => (typeof counts?.[id] === 'number' ? (counts[id] as number) : undefined)
  const toggle = (id: number) => setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  const allOnPage = items.length > 0 && items.every((s) => selected.includes(s.id))
  const sentToday = list.data?.sent_today ?? 0
  const limit = list.data?.daily_limit ?? 0
  const finding = running.includes('studios_find') || find.isPending
  const checking = running.includes('studio_contacts') || contacts.isPending

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Студии"
        meta={
          <>
            Студии разработки и дизайна, которым может пригодиться ваш опыт. Письмо пишет Claude, отправляете вы.
            {list.data && <div className={cx(sentToday > limit && 'font-medium text-bad')}>Отправлено сегодня: {sentToday} из {limit}</div>}
            {['studios_find', 'studio_contacts'].map((job) => {
              const run = lastRun(job)
              return run ? (
                <div key={job}>
                  {job === 'studios_find' ? 'Последний поиск' : 'Последняя проверка контактов'}: {formatDateTime(run.started_at)} — {run.status}
                  {run.error ? ` (${run.error})` : ''}
                </div>
              ) : null
            })}
          </>
        }
        actions={
          <>
            <Button variant="ghost" onClick={() => setSheet('settings')}>
              <Settings2 aria-hidden />
              Настройки
            </Button>
            <Button variant="ghost" onClick={() => setSheet('import')}>
              <ListPlus aria-hidden />
              Свой список
            </Button>
            <Button onClick={() => contacts.mutate(null)} disabled={checking} title="Новые студии и те, чей сайт не открылся (повторно — не раньше чем через 3 часа)">
              {checking ? 'Проверяю контакты…' : 'Проверить контакты'}
            </Button>
            <Button variant="primary" onClick={() => find.mutate()} disabled={finding}>
              <Search aria-hidden />
              {finding ? 'Ищу студии…' : 'Найти студии'}
            </Button>
          </>
        }
      />
      {(find.error || contacts.error) && <ErrorNotice error={find.error ?? contacts.error} title="Действие не выполнено" />}

      <Disclosure summary="Как это работает" className="rounded-lg border border-line bg-surface px-4 py-3 sm:px-5">
        <ul className="list-disc space-y-1 pl-5 text-caption text-ink-2">
          <li>Источники: поиск работодателей на hh.ru по ключевым словам (вся Россия), работодатели ваших подходящих вакансий, посты Telegram-каналов со ссылкой на сайт студии и ваш собственный список.</li>
          <li>Студии без цифровой работы (интерьер, ремонт, текстиль, печать на одежде) поиск пропускает. Контакты панель берёт со страниц сайта студии.</li>
          <li>Письмо под каждую студию пишет Claude в пакете для /hh-batch. Отправляете его вы, из своей почты или Telegram, и отмечаете здесь. Ответы на письма подхватывает синхронизация почты.</li>
          <li>Студиям с hh.ru можно отправить только инициативный отклик: соглашение hh разрешает использовать его данные лишь для поиска работы.</li>
        </ul>
      </Disclosure>

      <section aria-label="Список студий">
        <Tabs
          label="Группы студий"
          value={group}
          onChange={(id) => {
            setGroup(id)
            setPage(0)
            setSelected([])
          }}
          items={STUDIO_GROUPS.map(([id, label]) => ({ id, label, count: countFor(id) }))}
        />
        <div className="flex flex-wrap items-center gap-2 py-3">
          <SearchInput
            label="Поиск по названию, сайту и описанию"
            placeholder="Название, сайт, описание"
            value={q}
            onChange={(v) => {
              setQ(v)
              setPage(0)
            }}
          />
          <Select
            aria-label="Источник"
            wrapperClassName="w-full sm:w-auto"
            value={source}
            onChange={(e) => {
              setSource(e.target.value)
              setPage(0)
            }}
          >
            {SOURCES.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </Select>
          <Select
            aria-label="Направление"
            wrapperClassName="w-full sm:w-auto"
            value={kind}
            onChange={(e) => {
              setKind(e.target.value)
              setPage(0)
            }}
          >
            {KIND_OPTIONS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </Select>
          <ToggleChip
            pressed={withContacts}
            onToggle={() => {
              setWithContacts(!withContacts)
              setPage(0)
            }}
          >
            С контактами
          </ToggleChip>
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
          <Button variant="primary" className="sm:ml-auto" disabled={!selected.length || letters.isPending} onClick={() => letters.mutate(selected)}>
            Подготовить письма ({selected.length})
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
        {letters.error && <ErrorNotice className="mb-3" error={letters.error} title="Письма не поставлены в очередь" />}

        <div className="overflow-clip rounded-lg border border-line bg-surface">
          <div className={cx('hidden border-b border-line px-4 py-2 text-xs font-medium text-ink-3 md:grid md:items-center md:gap-x-4', ROW_GRID)}>
            <span>
              {items.length > 0 && (
                <input
                  type="checkbox"
                  className="size-4 cursor-pointer accent-accent"
                  aria-label="Выбрать все на странице"
                  checked={allOnPage}
                  onChange={() => setSelected(allOnPage ? [] : items.map((s) => s.id))}
                />
              )}
            </span>
            <span>Студия</span>
            <span>Направление</span>
            <span>Город</span>
            <span className="text-right">Совпадение</span>
            <span>Контакты</span>
            <span>Статус</span>
          </div>
          {list.isLoading && (
            <div aria-busy="true" aria-label="Загрузка студий" className="divide-y divide-rule">
              {Array.from({ length: 5 }, (_, i) => (
                <div key={i} className="space-y-2 px-4 py-3">
                  <Skeleton className="h-4 w-1/3" />
                  <Skeleton className="h-3 w-1/4" />
                </div>
              ))}
            </div>
          )}
          {list.error && <ErrorNotice className="m-4" error={list.error} title="Студии не загрузились" onRetry={() => void list.refetch()} />}
          {list.data && items.length === 0 && <EmptyState title="Здесь пусто">Нажмите «Найти студии» или добавьте свой список.</EmptyState>}
          {items.length > 0 && (
            <ul aria-label="Студии" className="divide-y divide-rule">
              {items.map((s) => (
                <StudioListRow key={s.id} studio={s} checked={selected.includes(s.id)} onToggle={() => toggle(s.id)} onOpen={() => setOpenId(s.id)} />
              ))}
            </ul>
          )}
          {list.data && <Pagination page={page} perPage={list.data.per_page} total={list.data.total} onPage={setPage} />}
        </div>
      </section>

      {openId !== null && <StudioDrawer id={openId} onClose={() => setOpenId(null)} onChanged={invalidate} />}
      {sheet === 'settings' && <SettingsSheet onClose={() => setSheet(null)} />}
      {sheet === 'import' && <ImportSheet onClose={() => setSheet(null)} onDone={invalidate} />}
    </div>
  )
}

function contactsText(s: StudioRow): string {
  const bits: string[] = []
  if (s.emails.length) bits.push(`почта: ${s.emails.length}`)
  if (s.telegram) bits.push('Telegram')
  return bits.join(' · ')
}

function StudioListRow({ studio: s, checked, onToggle, onOpen }: { studio: StudioRow; checked: boolean; onToggle: () => void; onOpen: () => void }) {
  const contacts = contactsText(s)
  const meta = [s.domain ?? '—', s.sources.map((x) => SOURCE_SHORT[x] ?? x).join(', '), s.open_vacancies ? `вакансий на hh: ${s.open_vacancies}` : null].filter(Boolean).join(' · ')
  return (
    <li className={cx('grid grid-cols-[1.25rem_minmax(0,1fr)] gap-x-3 px-4 py-3 transition-colors hover:bg-sunken md:items-center md:gap-x-4', ROW_GRID)}>
      <input type="checkbox" className="mt-0.5 size-4 cursor-pointer accent-accent md:mt-0" aria-label={`Выбрать: ${s.name}`} checked={checked} onChange={onToggle} />
      <div className="min-w-0">
        <button type="button" onClick={onOpen} className="min-w-0 rounded-sm text-left text-sm font-medium text-ink underline-offset-2 hover:underline">
          {s.name}
        </button>
        <div className="mt-0.5 truncate text-caption text-ink-2">{meta}</div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-ink-2 md:hidden">
          <span>{s.kind_label}</span>
          {s.area && (
            <>
              <span className="text-ink-3">·</span>
              <span>{s.area}</span>
            </>
          )}
          {s.fit_score !== null && (
            <>
              <span className="text-ink-3">·</span>
              <span>совпадение {s.fit_score}</span>
            </>
          )}
          <Badge tone={STATUS_TONE[s.status] ?? 'neutral'}>{s.status_label}</Badge>
        </div>
      </div>
      <span className="hidden text-caption text-ink-2 md:block">{s.kind_label}</span>
      <span className="hidden truncate text-caption text-ink-2 md:block">{s.area ?? '—'}</span>
      <span className="hidden text-right text-sm tabular-nums text-ink md:block" title={s.fit_notes.join('\n') || undefined}>
        {s.fit_score ?? '—'}
      </span>
      <span className="hidden text-caption text-ink-2 md:block">{contacts || <span className="text-ink-3">{s.contacts_checked_at ? 'нет' : 'не проверены'}</span>}</span>
      <span className="hidden md:block">
        <Badge tone={STATUS_TONE[s.status] ?? 'neutral'}>{s.status_label}</Badge>
      </span>
    </li>
  )
}

function SheetHeader({ id, title }: { id: string; title: string }) {
  return (
    <div className="flex h-12 shrink-0 items-center justify-between border-b border-line pl-5 pr-3">
      <h2 id={id} className="text-[15px] font-semibold">
        {title}
      </h2>
      <SheetCloseButton />
    </div>
  )
}

function SettingsSheet({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const settings = useQuery({ queryKey: ['studio-settings'], queryFn: () => apiGet<StudioSettings>('/api/studios/settings') })
  const [draft, setDraft] = useState<StudioSettings | null>(null)
  useEffect(() => {
    if (settings.data && draft === null) setDraft(settings.data)
  }, [settings.data, draft])
  const save = useMutation({
    mutationFn: (body: StudioSettings) => apiPut<StudioSettings>('/api/studios/settings', body),
    onSuccess: (data) => {
      setDraft(data)
      void qc.invalidateQueries({ queryKey: ['studio-settings'] })
    },
  })
  return (
    <Sheet size="md" labelledBy="studio-settings-title" onClose={onClose}>
      <SheetHeader id="studio-settings-title" title="Настройки поиска и писем" />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        {settings.error && <ErrorNotice error={settings.error} title="Настройки не загрузились" onRetry={() => void settings.refetch()} />}
        {!draft ? (
          !settings.error && <Skeleton className="h-24 w-full" />
        ) : (
          <form
            className="space-y-5"
            onSubmit={(e) => {
              e.preventDefault()
              save.mutate(draft)
            }}
          >
            <Field label="Ключевые слова поиска на hh.ru" hint="Ищутся в названии и описании работодателя.">
              <TagInput label="Ключевые слова поиска на hh.ru" value={draft.keywords} onChange={(keywords) => setDraft({ ...draft, keywords })} placeholder="веб-студия, дизайн-студия…" />
            </Field>
            <Checkbox
              label="Только студии с открытыми вакансиями на hh.ru (они сейчас нанимают)"
              checked={draft.only_with_vacancies}
              onChange={(e) => setDraft({ ...draft, only_with_vacancies: e.target.checked })}
            />
            <Field label="Подпись" hint="Панель добавит её в конец каждого письма.">
              <Textarea className="min-h-20" value={draft.signature} onChange={(e) => setDraft({ ...draft, signature: e.target.value })} placeholder={'Имя Фамилия\nTelegram, телефон'} />
            </Field>
            <Field label="Для предложения услуг: форматы и ставки" hint="Claude берёт цены только отсюда.">
              <Textarea className="min-h-20" value={draft.offer_notes} onChange={(e) => setDraft({ ...draft, offer_notes: e.target.value })} placeholder="Проектная работа, почасовая ставка…" />
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

function ImportSheet({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('')
  const [letterKind, setLetterKind] = useState<StudioLetterKind>('application')
  const run = useMutation({
    mutationFn: () => apiPost<{ added: number; updated: number; errors: string[] }>('/api/studios/import', { text, letter_kind: letterKind }),
    onSuccess: () => {
      setText('')
      onDone()
    },
  })
  return (
    <Sheet size="md" labelledBy="studio-import-title" onClose={onClose}>
      <SheetHeader id="studio-import-title" title="Добавить свой список студий" />
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            run.mutate()
          }}
        >
          <p className="text-caption text-ink-2">
            По строке на студию: «сайт», «название; сайт» или «название; сайт; почта». Подойдёт список, собранный в каталогах (Tagline, Рейтинг Рунета, Behance).
          </p>
          <Textarea aria-label="Список студий" className="min-h-40 font-mono text-xs" value={text} onChange={(e) => setText(e.target.value)} placeholder={'pixel-studio.example\nСтудия Код; kod-studio.example; hello@kod-studio.example'} />
          <Field label="Какое письмо готовить">
            <Select value={letterKind} onChange={(e) => setLetterKind(e.target.value as StudioLetterKind)}>
              <option value="application">Инициативный отклик</option>
              <option value="offer">Предложение услуг</option>
            </Select>
          </Field>
          <Button type="submit" variant="primary" disabled={run.isPending || text.trim().length < 3}>
            {run.isPending ? 'Добавляю…' : 'Добавить'}
          </Button>
          {run.data && (
            <p role="status" className="text-caption text-ink-2">
              Добавлено {run.data.added}, обновлено {run.data.updated}
              {run.data.errors.length ? `. Ошибки: ${run.data.errors.join('; ')}` : ''}
            </p>
          )}
          {run.error && <ErrorNotice error={run.error} title="Список не добавлен" />}
        </form>
      </div>
    </Sheet>
  )
}

export function StudioDrawer({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const qc = useQueryClient()
  const detail = useQuery({ queryKey: ['studio', id], queryFn: () => apiGet<StudioDetail>(`/api/studios/${id}`) })
  const d = detail.data
  const [emails, setEmails] = useState('')
  const [telegram, setTelegram] = useState('')
  const [to, setTo] = useState('')
  const [via, setVia] = useState('email')
  const [notes, setNotes] = useState('')
  const [sentInfo, setSentInfo] = useState('')
  const [copied, setCopied] = useState<'subject' | 'letter' | null>(null)
  // the draft on screen: the style the user switched to, else the current (picked) one
  const [style, setStyle] = useState<StudioLetterStyle | null>(null)
  const variants = d ? (d.variants?.length ? d.variants : d.letter ? [d.letter] : []) : []
  const shown = variants.find((v) => v.style === style) ?? d?.letter ?? variants[0] ?? null
  // what the user typed, kept with the id of the draft it belongs to: another tab or a new version shows its own text,
  // a refetch of the same draft keeps the typing; the editor never renders empty before an effect fills it
  const shownId = shown?.id ?? null
  const [edit, setEdit] = useState<{ id: number | null; subject: string; text: string } | null>(null)
  const typed = edit && edit.id === shownId ? edit : null
  const subject = typed ? typed.subject : (shown?.subject ?? '')
  const text = typed ? typed.text : (shown?.text ?? '')
  const setSubject = (value: string) => setEdit({ id: shownId, subject: value, text })
  const setText = (value: string) => setEdit({ id: shownId, subject, text: value })
  useEffect(() => {
    if (!d) return
    setEmails(d.emails.join(', '))
    setTelegram(d.telegram ?? '')
    setTo((current) => current || d.emails[0] || d.telegram || '')
  }, [d])
  // «скопировано» belongs to the draft it was said about
  useEffect(() => setCopied(null), [shownId])
  const refresh = (data?: StudioDetail) => {
    if (data) qc.setQueryData(['studio', id], data)
    onChanged()
  }
  const act = useMutation({
    mutationFn: ({ path, body, method }: { path: string; body?: unknown; method?: 'put' }) =>
      method === 'put' ? apiPut<StudioDetail>(`/api/studios/${id}${path}`, body) : apiPost<StudioDetail>(`/api/studios/${id}${path}`, body),
    onSuccess: (data) => refresh(data),
  })
  const draft = useMutation({
    mutationFn: () => apiPost<{ queued: number; skipped: string[]; batch_id: string | null }>('/api/studios/letters', { ids: [id], notes: notes || null }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['studio', id] })
      onChanged()
    },
  })
  const markSent = useMutation({
    mutationFn: () => apiPost<StudioDetail>(`/api/studios/${id}/sent`, { to, via, letter_id: shown?.id ?? null }),
    onSuccess: (data) => {
      setSentInfo(
        `Отправлено сегодня: ${data.sent_today} из ${data.daily_limit}` + (data.over_limit ? ' — больше дневного лимита: почтовые службы могут счесть ящик спамерским, остальное лучше завтра' : ''),
      )
      refresh(data)
    },
  })
  const recheck = useMutation({ mutationFn: () => apiPost('/api/studios/contacts', { ids: [id] }), onSuccess: () => onChanged() })
  const copy = async (what: 'subject' | 'letter', value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(what)
    } catch {
      /* the text stays selectable in the editor */
    }
  }
  const error = act.error ?? draft.error ?? markSent.error ?? recheck.error

  return (
    <Sheet onClose={onClose} labelledBy="studio-title">
      <div className="flex h-12 shrink-0 items-center gap-1 border-b border-line px-3 sm:px-4">
        <span className="text-caption text-ink-3">Студия</span>
        <div className="ml-auto flex items-center gap-1">
          {d?.site_url && (
            <a className={buttonClass('ghost', 'sm')} href={d.site_url} target="_blank" rel="noreferrer">
              {d.domain ?? 'Сайт'}
              <ExternalLink aria-hidden />
            </a>
          )}
          {d?.hh_url && (
            <a className={buttonClass('ghost', 'sm')} href={d.hh_url} target="_blank" rel="noreferrer">
              hh.ru
              <ExternalLink aria-hidden />
            </a>
          )}
          <SheetCloseButton />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {detail.isLoading && (
          <div className="space-y-3 px-6 py-5">
            <Skeleton className="h-6 w-1/2" />
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="mt-6 h-40 w-full" />
          </div>
        )}
        {detail.error && <ErrorNotice className="m-5" error={detail.error} title="Не удалось открыть студию" onRetry={() => void detail.refetch()} />}
        {d && (
          <div className="space-y-6 px-4 pb-10 pt-5 sm:px-6">
            <header className="space-y-3">
              <div>
                <h2 id="studio-title" className="text-lg font-semibold leading-snug text-ink">
                  {d.name}
                </h2>
                <div className="mt-1 text-sm text-ink-2">
                  {d.kind_label} · {d.area ?? 'город не указан'}
                  {d.open_vacancies ? ` · вакансий на hh: ${d.open_vacancies}` : ''}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={STATUS_TONE[d.status] ?? 'neutral'}>{d.status_label}</Badge>
                {d.status === 'sent' && (
                  <>
                    <Button size="sm" onClick={() => act.mutate({ path: '/replied' })} disabled={act.isPending}>
                      Ответили
                    </Button>
                    <Button size="sm" onClick={() => act.mutate({ path: '/declined' })} disabled={act.isPending}>
                      Отказ
                    </Button>
                  </>
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
              {d.sent_at && (
                <p className="text-caption text-ink-2">
                  Отправлено {formatDateTime(d.sent_at)} ({d.sent_via}: {d.sent_to ?? '—'}){d.letter ? ` · стиль: ${d.letter.style_label}` : ''}
                  {d.replied_at ? ` · ответ ${formatDateTime(d.replied_at)}${d.reply_subject ? `: «${d.reply_subject}»` : ''}` : ''}
                </p>
              )}
              {error && <ErrorNotice error={error} title="Действие не выполнено" />}
            </header>

            {(d.fit_notes.length > 0 || d.services.length > 0 || d.site_summary || d.description) && (
              <section className="space-y-2 text-sm text-ink-2">
                {d.fit_notes.length > 0 && (
                  <p>
                    <span className="font-medium text-ink">Совпадение {d.fit_score}:</span> {d.fit_notes.join(' · ')}
                  </p>
                )}
                {d.services.length > 0 && (
                  <p>
                    <span className="font-medium text-ink">Услуги:</span> {d.services.join(', ')}
                  </p>
                )}
                {d.site_summary && (
                  <p>
                    <span className="font-medium text-ink">С сайта:</span> {d.site_summary}
                  </p>
                )}
                {d.description && <p className="whitespace-pre-line">{d.description}</p>}
              </section>
            )}
            {d.vacancies.length > 0 && (
              <section>
                <h3 className="mb-1.5 text-sm font-semibold">Её вакансии в панели</h3>
                <ul className="list-disc space-y-0.5 pl-5 text-sm">
                  {d.vacancies.map((v) => (
                    <li key={v.id}>
                      <Link className="font-medium text-accent underline-offset-2 hover:underline" to={`/vacancies?open=${v.id}`}>
                        {v.name}
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="space-y-3 border-t border-line pt-5">
              <h3 className="text-sm font-semibold">Контакты</h3>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Почта, через запятую">
                  <Input value={emails} onChange={(e) => setEmails(e.target.value)} />
                </Field>
                <Field label="Telegram">
                  <Input value={telegram} onChange={(e) => setTelegram(e.target.value)} placeholder="https://t.me/…" />
                </Field>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  onClick={() =>
                    act.mutate({
                      path: '',
                      method: 'put',
                      body: {
                        emails: emails
                          .split(',')
                          .map((s) => s.trim())
                          .filter(Boolean),
                        telegram,
                      },
                    })
                  }
                  disabled={act.isPending}
                >
                  Сохранить контакты
                </Button>
                {d.site_url && (
                  <Button variant="ghost" onClick={() => recheck.mutate()} disabled={recheck.isPending}>
                    Проверить сайт ещё раз
                  </Button>
                )}
                {d.contact_url && (
                  <a className={buttonClass('ghost')} href={d.contact_url} target="_blank" rel="noreferrer">
                    Страница контактов
                    <ExternalLink aria-hidden />
                  </a>
                )}
              </div>
              {d.contacts_error && <p className="text-caption text-warn">{d.contacts_error}</p>}
            </section>

            <section className="space-y-3 border-t border-line pt-5">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-sm font-semibold">Письмо</h3>
                <Select
                  aria-label="Вид письма"
                  wrapperClassName="w-auto"
                  value={d.letter_kind}
                  onChange={(e) => act.mutate({ path: '', method: 'put', body: { letter_kind: e.target.value } })}
                >
                  <option value="application">{LETTER_KIND_LABELS.application}</option>
                  <option value="offer" disabled={!d.offer_allowed}>
                    {LETTER_KIND_LABELS.offer}
                    {d.offer_allowed ? '' : ' — нельзя для студий с hh.ru'}
                  </option>
                </Select>
                {d.request && ['queued', 'sent_in_batch'].includes(d.request.status) && (
                  <Badge tone="info">{d.request.batch_id ? `в пакете ${d.request.batch_id} — запустите /hh-batch` : 'ждёт следующего пакета'}</Badge>
                )}
                {d.request?.status === 'cancelled' && d.request.error && <span className="text-caption text-warn">Claude: {d.request.error}</span>}
              </div>
              {shown ? (
                <>
                  {variants.length > 1 && (
                    <div>
                      <Tabs label="Стиль письма" value={shown.style} onChange={(s) => setStyle(s as StudioLetterStyle)} items={variants.map((v) => ({ id: v.style, label: STYLE_TABS[v.style] ?? v.style_label }))} />
                      <p className="mt-2 text-caption text-ink-3">Два черновика: отправьте тот, что больше нравится. Кнопки ниже работают с открытым, «Отметить отправленным» запомнит, какой ушёл.</p>
                    </div>
                  )}
                  {shown.needs_clarification.length > 0 && (
                    <Notice tone="warn" title="Уточните перед отправкой">
                      <ul className="list-disc pl-4">
                        {shown.needs_clarification.map((c) => (
                          <li key={c}>{c}</li>
                        ))}
                      </ul>
                    </Notice>
                  )}
                  {shown.warnings.length > 0 && (
                    <Notice tone="warn" title="Замечания к письму">
                      {shown.warnings.join('; ')}
                    </Notice>
                  )}
                  <Field label="Тема">
                    <Input value={subject} onChange={(e) => setSubject(e.target.value)} aria-label="Тема письма" />
                  </Field>
                  <Textarea className="min-h-64 text-prose" value={text} onChange={(e) => setText(e.target.value)} aria-label="Текст письма" />
                  <p className="whitespace-pre-line rounded-lg bg-sunken px-3 py-2 text-caption text-ink-2">
                    Подпись в конце: {shown.full_text.slice(shown.text.length).trim() || 'не задана (кнопка «Настройки»)'}
                  </p>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant={subject !== shown.subject || text !== shown.text ? 'primary' : 'secondary'}
                      onClick={() => act.mutate({ path: '/letter', method: 'put', body: { subject, text, style: shown.style } })}
                      disabled={(subject === shown.subject && text === shown.text) || act.isPending}
                    >
                      Сохранить правку
                    </Button>
                    {d.emails[0] && (
                      <a className={buttonClass('secondary')} href={mailtoHref(to.includes('@') ? to : d.emails[0], shown.subject, shown.full_text)}>
                        <Mail aria-hidden />
                        Открыть в почте
                      </a>
                    )}
                    <Button variant="ghost" onClick={() => void copy('subject', shown.subject)}>
                      <Copy aria-hidden />
                      {copied === 'subject' ? 'Тема скопирована' : 'Скопировать тему'}
                    </Button>
                    <Button variant="ghost" onClick={() => void copy('letter', shown.full_text)}>
                      <Copy aria-hidden />
                      {copied === 'letter' ? 'Письмо скопировано' : 'Скопировать письмо'}
                    </Button>
                  </div>
                </>
              ) : (
                <p className="text-caption text-ink-2">Письма ещё нет.</p>
              )}
              {['new', 'ready', 'no_contacts', 'drafted'].includes(d.status) && (
                <form
                  className="flex flex-col gap-2 sm:flex-row"
                  onSubmit={(e) => {
                    e.preventDefault()
                    draft.mutate()
                  }}
                >
                  <Input aria-label="Пожелание к письму" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Пожелание к письму, необязательно" />
                  <Button type="submit" variant={d.letter ? 'secondary' : 'primary'} disabled={draft.isPending}>
                    {d.letter ? 'Переписать через Claude' : 'Подготовить письмо'}
                  </Button>
                </form>
              )}
            </section>

            {d.letter && !['sent', 'replied', 'declined'].includes(d.status) && (
              <section className="space-y-3 rounded-lg border border-good/25 bg-good-soft p-4">
                <h3 className="text-sm font-semibold">Отправили? Отметьте</h3>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <Input value={to} onChange={(e) => setTo(e.target.value)} placeholder="Кому" aria-label="Кому" className="bg-surface" />
                  <Select aria-label="Как отправили" wrapperClassName="w-full sm:w-56" className="bg-surface" value={via} onChange={(e) => setVia(e.target.value)}>
                    <option value="email">Почтой</option>
                    <option value="telegram">В Telegram</option>
                    <option value="form">Через форму на сайте</option>
                    <option value="other">Иначе</option>
                  </Select>
                  <Button variant="primary" onClick={() => markSent.mutate()} disabled={markSent.isPending}>
                    Отметить отправленным
                  </Button>
                </div>
              </section>
            )}
            {sentInfo && (
              <Notice tone={sentInfo.includes('больше дневного лимита') ? 'warn' : 'good'} role="status">
                {sentInfo}
              </Notice>
            )}
          </div>
        )}
      </div>
    </Sheet>
  )
}
