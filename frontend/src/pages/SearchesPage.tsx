import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, MoreHorizontal, Plus, RefreshCw } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { apiDelete, apiGet, apiPost, apiPut, errorMessage } from '../api/client'
import type { JobsView, Option, PreviewResult, SavedSearch, SearchInput as SearchForm, SearchOptions, SearchParams, SelectionProfile } from '../api/types'
import RefPicker from '../components/RefPicker'
import { Tag } from '../components/TagInput'
import { Badge, type BadgeTone } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { ToggleChip } from '../components/ui/Chip'
import { cx } from '../components/ui/cx'
import { Checkbox, Field, Input, Select } from '../components/ui/Field'
import { Menu } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Panel } from '../components/ui/Panel'
import { SearchInput } from '../components/ui/SearchInput'
import { Sheet, SheetCloseButton, useSheet } from '../components/ui/Sheet'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { countOf, formatDateTime, shortAgo } from '../lib/format'
import { money } from '../lib/money'

const EMPTY_PARAMS: SearchParams = {
  text: '',
  search_field: [],
  experience: [],
  employment_form: [],
  work_format: [],
  work_schedule_by_days: [],
  salary: null,
  currency: 'RUR',
  only_with_salary: false,
  label: [],
  professional_role: [],
  industry: [],
  areas: [],
  excluded_text: '',
  education: [],
  period_days: null,
}

const EMPTY: SearchForm = { name: '', source: 'search', params: EMPTY_PARAMS, interval_min: 30, enabled: true, watch_only: false, selection_profile_id: null }

const RUN_TONE: Record<string, BadgeTone> = { ok: 'good', running: 'info', error: 'bad' }
const RUN_LABEL: Record<string, string> = { ok: 'готово', running: 'идёт', error: 'ошибка' }

/** «Поиски»: the saved hh.ru searches the scanner runs, their last results, and the form of a search in a side sheet. */
export default function SearchesPage() {
  const qc = useQueryClient()
  const searches = useQuery({ queryKey: ['searches'], queryFn: () => apiGet<SavedSearch[]>('/api/searches'), refetchInterval: 15000 })
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => apiGet<JobsView>('/api/jobs?limit=5'), refetchInterval: 5000 })
  const options = useQuery({ queryKey: ['search-options'], queryFn: () => apiGet<SearchOptions>('/api/searches/options'), staleTime: 3_600_000, retry: 0 })
  const [editing, setEditing] = useState<SearchForm | null>(null)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [filter, setFilter] = useState('')

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['searches'] })
    void qc.invalidateQueries({ queryKey: ['jobs'] })
  }
  const run = useMutation({ mutationFn: (id: number) => apiPost<{ job_run_id: number }>(`/api/searches/${id}/run`), onSuccess: invalidate })
  const runAll = useMutation({ mutationFn: () => apiPost<{ job_run_id: number }>('/api/jobs/scan'), onSuccess: invalidate })
  const remove = useMutation({ mutationFn: (id: number) => apiDelete(`/api/searches/${id}`), onSuccess: invalidate })
  const toggle = useMutation({
    mutationFn: (s: SavedSearch) =>
      apiPut<SavedSearch>(`/api/searches/${s.id}`, {
        name: s.name,
        source: s.source,
        params: s.params,
        interval_min: s.interval_min,
        enabled: !s.enabled,
        watch_only: s.watch_only,
        selection_profile_id: s.selection_profile_id,
      }),
    onSuccess: invalidate,
  })

  const scanning = jobs.data?.running.includes('scan') ?? false
  const all = searches.data ?? []
  const needle = filter.trim().toLowerCase()
  const shown = needle ? all.filter((s) => `${s.name} ${s.summary} ${s.selection_profile_name ?? ''}`.toLowerCase().includes(needle)) : all
  const enabled = all.filter((s) => s.enabled).length
  const openForm = (form: SearchForm, id: number | null) => {
    setEditing(form)
    setEditingId(id)
  }
  const error = runAll.error ?? run.error ?? remove.error ?? toggle.error

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Поиски"
        meta={
          searches.data &&
          `${countOf(all.length, ['поиск', 'поиска', 'поисков'])}, включено ${enabled}${jobs.data?.last_scan_at ? ` · последнее сканирование ${shortAgo(jobs.data.last_scan_at)}` : ''}`
        }
        actions={
          <>
            <Button onClick={() => runAll.mutate()} disabled={scanning || runAll.isPending}>
              <RefreshCw aria-hidden className={cx((scanning || runAll.isPending) && 'animate-spin motion-reduce:animate-none')} />
              {scanning ? 'Идёт сканирование…' : 'Сканировать всё'}
            </Button>
            <Button variant="primary" onClick={() => openForm({ ...EMPTY, params: { ...EMPTY_PARAMS } }, null)}>
              <Plus aria-hidden />
              Новый поиск
            </Button>
          </>
        }
      />
      {error && <ErrorNotice error={error} title="Действие не выполнено" />}
      {options.error && (
        <Notice tone="warn" title="Справочники hh.ru недоступны">
          {errorMessage(options.error)}. Форма поиска покажет только базовые поля.
        </Notice>
      )}

      <Panel
        title="Поиски"
        flush
        actions={all.length > 8 && <SearchInput label="Найти поиск по названию или параметрам" placeholder="Найти поиск" value={filter} onChange={setFilter} delay={0} className="w-full sm:w-64" />}
      >
        {searches.isLoading && (
          <div aria-busy="true" aria-label="Загрузка поисков" className="space-y-3 px-5 pb-5">
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-4 w-2/5" />
            <Skeleton className="h-4 w-1/3" />
          </div>
        )}
        {searches.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={searches.error} title="Поиски не загрузились" onRetry={() => void searches.refetch()} />}
        {searches.data && all.length === 0 && (
          <EmptyState
            className="pt-2"
            title="Пока нет поисков"
            action={
              <Button variant="primary" onClick={() => openForm({ ...EMPTY, params: { ...EMPTY_PARAMS } }, null)}>
                Новый поиск
              </Button>
            }
          >
            Задайте текст запроса и фильтры: панель будет проверять hh.ru с выбранным интервалом.
          </EmptyState>
        )}
        {all.length > 0 && shown.length === 0 && <EmptyState inset="panel" title="Ничего не найдено">Нет поиска, в названии или параметрах которого есть «{filter.trim()}».</EmptyState>}
        {shown.length > 0 && (
          <Table label="Поиски" minWidth={880}>
            <thead>
              <tr>
                <Th className="sticky left-0 z-[1] bg-surface">Название</Th>
                <Th>Параметры</Th>
                <Th align="right">Интервал</Th>
                <Th>Последний прогон</Th>
                <Th align="right">Найдено / новых</Th>
                <Th>
                  <span className="sr-only">Действия</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {shown.map((s) => {
                const edit = () =>
                  openForm(
                    { name: s.name, source: s.source, params: s.params, interval_min: s.interval_min, enabled: s.enabled, watch_only: s.watch_only, selection_profile_id: s.selection_profile_id },
                    s.id,
                  )
                return (
                <tr key={s.id} className={cx(!s.enabled && 'text-ink-3')}>
                  {/* the name stays in place while a phone scrolls the table sideways */}
                  <Td className="sticky left-0 z-[1] w-[13rem] min-w-[11rem] bg-surface max-sm:shadow-[1px_0_0_var(--color-line)]">
                    <div className="font-medium text-ink">
                      <button type="button" className="rounded-sm text-left underline-offset-2 hover:underline" onClick={edit} title="Изменить поиск">
                        {s.name}
                      </button>
                      {!s.enabled && (
                        <Badge tone="outline" className="ml-2 align-[1px]">
                          выключен
                        </Badge>
                      )}
                    </div>
                    <div className="text-caption text-ink-3">
                      {s.source === 'similar_to_resume' ? 'под резюме' : s.watch_only ? 'наблюдение: ссылки в бота без разбора' : 'поиск'}
                      {s.selection_profile_name ? ` · критерии: ${s.selection_profile_name}` : ''}
                    </div>
                  </Td>
                  <Td className="min-w-[16rem] text-caption text-ink-2 wrap-anywhere">{s.summary}</Td>
                  <Td align="right" className="whitespace-nowrap text-ink-2">
                    {s.interval_min} мин
                  </Td>
                  <Td className="whitespace-nowrap text-ink-2">
                    <span className="tabular-nums" title={formatDateTime(s.last_run_at)}>
                      {s.last_run_at ? shortAgo(s.last_run_at) : '—'}
                    </span>
                    {s.last_error && <div className="max-w-xs whitespace-normal text-caption text-bad">{s.last_error}</div>}
                    {s.last_warning && <div className="max-w-xs whitespace-normal text-caption text-warn">{s.last_warning}</div>}
                  </Td>
                  <Td align="right" className="whitespace-nowrap">
                    {s.last_found ?? '—'} / {s.last_new ?? '—'}
                    {s.last_requests !== null && <div className="text-xs text-ink-3">{s.last_requests} запр.</div>}
                  </Td>
                  <Td className="w-px whitespace-nowrap">
                    <div className="flex items-center justify-end gap-1">
                      <Button size="sm" variant="ghost" disabled={scanning || run.isPending} onClick={() => run.mutate(s.id)}>
                        Запустить
                      </Button>
                      <Menu
                        label={`Действия с поиском «${s.name}»`}
                        items={[
                          { key: 'edit', label: 'Изменить', onSelect: edit },
                          { key: 'toggle', label: s.enabled ? 'Выключить' : 'Включить', onSelect: () => toggle.mutate(s) },
                          'separator',
                          {
                            key: 'remove',
                            label: 'Удалить',
                            tone: 'danger',
                            onSelect: () => {
                              if (window.confirm(`Удалить поиск «${s.name}»?`)) remove.mutate(s.id)
                            },
                          },
                        ]}
                        trigger={(p) => (
                          <Button {...p} variant="ghost" size="sm" iconOnly aria-label={`Действия с поиском «${s.name}»`} title="Действия">
                            <MoreHorizontal aria-hidden />
                          </Button>
                        )}
                      />
                    </div>
                  </Td>
                </tr>
                )
              })}
            </tbody>
          </Table>
        )}
      </Panel>

      <Panel title="Последние запуски сканера" flush>
        {jobs.data && jobs.data.runs.length === 0 && <EmptyState inset="panel" title="Сканер ещё не запускался" />}
        {jobs.data && jobs.data.runs.length > 0 && (
          <ul className="divide-y divide-rule">
            {jobs.data.runs.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-sm sm:px-5">
                <Badge tone={RUN_TONE[r.status] ?? 'neutral'}>{RUN_LABEL[r.status] ?? r.status}</Badge>
                <span className="text-caption tabular-nums text-ink-3">
                  {formatDateTime(r.started_at)}
                  {r.duration_sec !== null && ` · ${r.duration_sec} с`}
                </span>
                <span className="text-ink-2">{summarizeStats(r.stats)}</span>
                {r.error && <span className="text-caption text-bad">{r.error}</span>}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {editing && (
        <SearchSheet
          initial={editing}
          options={options.data}
          editingId={editingId}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            invalidate()
          }}
        />
      )}
    </div>
  )
}

function summarizeStats(stats: Record<string, unknown> | null): string {
  if (!stats) return ''
  const parts: string[] = []
  const searches = stats.searches as { found?: number; new?: number; error?: string }[] | undefined
  if (searches) parts.push(`поисков ${searches.length}, новых ${searches.reduce((a, s) => a + (s.new ?? 0), 0)}`)
  const p = stats.processed as { processed?: number; prescored?: number; filtered_out?: number; not_suitable?: number } | undefined
  if (p) parts.push(`обработано ${p.processed}: кандидатов ${p.prescored}, отфильтровано ${p.filtered_out}, ниже порога ${p.not_suitable}`)
  if (stats.skipped) parts.push(String(stats.skipped))
  if (stats.aborted) parts.push(`остановлено: ${String(stats.aborted)}`)
  return parts.join(' · ')
}

/** Multi-select of a handful of options: round toggles, pressed ones tinted. */
function ChipChoice({ label, options, value, onChange }: { label: string; options: Option[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <fieldset className="min-w-0">
      <legend className="mb-1.5 text-caption font-medium text-ink-2">{label}</legend>
      {options.length === 0 ? (
        <p className="text-caption text-ink-3">Справочник недоступен.</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {options.map((o) => (
            <ToggleChip key={o.id} pressed={value.includes(o.id)} onToggle={() => onChange(value.includes(o.id) ? value.filter((x) => x !== o.id) : [...value, o.id])}>
              {o.name}
            </ToggleChip>
          ))}
        </div>
      )}
    </fieldset>
  )
}

function FormSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-4 border-t border-line pt-5 first:border-t-0 first:pt-0">
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      {children}
    </section>
  )
}

function SearchSheet({
  initial,
  options,
  editingId,
  onClose,
  onSaved,
}: {
  initial: SearchForm
  options: SearchOptions | undefined
  editingId: number | null
  onClose: () => void
  onSaved: () => void
}) {
  return (
    <Sheet labelledBy="search-form-title" onClose={onClose}>
      <SearchFormBody initial={initial} options={options} editingId={editingId} onSaved={onSaved} />
    </Sheet>
  )
}

function SearchFormBody({ initial, options, editingId, onSaved }: { initial: SearchForm; options: SearchOptions | undefined; editingId: number | null; onSaved: () => void }) {
  const { close } = useSheet()
  const [form, setForm] = useState<SearchForm>(initial)
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const sets = useQuery({ queryKey: ['selection-profiles'], queryFn: () => apiGet<{ items: SelectionProfile[] }>('/api/selection-profiles') })
  const p = form.params
  const setP = <K extends keyof SearchParams>(key: K, value: SearchParams[K]) => setForm({ ...form, params: { ...form.params, [key]: value } })

  const save = useMutation({
    mutationFn: () => (editingId ? apiPut<SavedSearch>(`/api/searches/${editingId}`, form) : apiPost<SavedSearch>('/api/searches', form)),
    onSuccess: onSaved,
  })
  const check = useMutation({
    mutationFn: () => apiPost<PreviewResult>('/api/searches/preview', { params: form.params, source: form.source }),
    onSuccess: setPreview,
  })

  const fallback = (ids: string[], labels?: Record<string, string>): Option[] => ids.map((id) => ({ id, name: labels?.[id] ?? id }))
  const workFormats = options?.work_format ?? fallback(['REMOTE', 'HYBRID', 'ON_SITE', 'FIELD_WORK'])
  const employment = options?.employment_form ?? fallback(['FULL', 'PART', 'PROJECT'])
  const experience = options?.experience ?? fallback(['noExperience', 'between1And3', 'between3And6', 'moreThan6'])
  const labels = options?.vacancy_label ?? fallback(['not_from_agency', 'accredited_it', 'with_salary'])
  const searchFields = options?.search_field ?? fallback(['name', 'company_name', 'description'])
  const currencies = options?.currency ?? fallback(['RUR', 'USD', 'EUR'])

  return (
    <>
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-line pl-5 pr-3">
        <h2 id="search-form-title" className="text-[15px] font-semibold">
          {editingId ? 'Изменить поиск' : 'Новый поиск'}
        </h2>
        <SheetCloseButton />
      </div>
      <form
        id="search-form"
        className="min-h-0 flex-1 space-y-6 overflow-y-auto overscroll-contain px-5 py-5 sm:px-6"
        onSubmit={(e) => {
          e.preventDefault()
          if (form.name.trim()) save.mutate()
        }}
      >
        <FormSection title="Основное">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Название" className="sm:col-span-2">
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Python remote Москва" />
            </Field>
            <Field label="Источник">
              <Select value={form.source} onChange={(e) => setForm({ ...form, source: e.target.value as SearchForm['source'] })}>
                <option value="search">Поиск по вакансиям</option>
                <option value="similar_to_resume" disabled={options ? !options.similar_available : false}>
                  Подходящие под резюме{options && !options.similar_available ? ' (недоступно)' : ''}
                </option>
              </Select>
            </Field>
            <Field label="Интервал, мин" hint="Не меньше 10.">
              <Input type="number" min={10} max={1440} value={form.interval_min} onChange={(e) => setForm({ ...form, interval_min: Number(e.target.value) })} />
            </Field>
            <Field label="Критерии отбора" hint="По этому набору фильтруются и оцениваются вакансии этого поиска; наборы настраиваются на странице «Профиль и критерии»." className="sm:col-span-2">
              <Select value={form.selection_profile_id ?? ''} onChange={(e) => setForm({ ...form, selection_profile_id: e.target.value ? Number(e.target.value) : null })}>
                <option value="">Основная анкета</option>
                {(sets.data?.items ?? []).map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <div>
            <Checkbox label="Только наблюдение" checked={!!form.watch_only} onChange={(e) => setForm({ ...form, watch_only: e.target.checked })} />
            <p className="ml-6 mt-0.5 text-caption text-ink-3">Новые вакансии уходят в бота ссылками, по 10 в сообщении, без правил, предоценки и разбора: для ролей вне анкеты, например склад.</p>
          </div>
        </FormSection>

        <FormSection title="Запрос">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label="Текст запроса"
              hint={
                <>
                  Язык запросов hh: AND, OR, NOT, кавычки для точной фразы, * для окончаний.{' '}
                  <a className="inline-flex items-center gap-0.5 font-medium text-accent underline-offset-2 hover:underline" href="https://hh.ru/article/1175" target="_blank" rel="noreferrer">
                    Справка
                    <ExternalLink aria-hidden className="size-3" />
                  </a>
                </>
              }
            >
              <Input value={p.text} onChange={(e) => setP('text', e.target.value)} placeholder='python AND (fastapi OR django) NOT "1С"' />
            </Field>
            <Field label="Исключить слова" hint="Через запятую.">
              <Input value={p.excluded_text} onChange={(e) => setP('excluded_text', e.target.value)} placeholder="стажёр, 1С" />
            </Field>
          </div>
          <ChipChoice label="Где искать текст" options={searchFields} value={p.search_field} onChange={(v) => setP('search_field', v)} />
          <Field label="Регионы">
            <RefPicker label="Регионы" value={p.areas} onChange={(v) => setP('areas', v)} endpoint="/api/searches/suggest/areas" placeholder="Начните вводить: Москва" />
          </Field>
        </FormSection>

        <FormSection title="Условия">
          <ChipChoice label="Формат работы" options={workFormats} value={p.work_format} onChange={(v) => setP('work_format', v)} />
          <ChipChoice label="Занятость" options={employment} value={p.employment_form} onChange={(v) => setP('employment_form', v)} />
          <ChipChoice label="Опыт" options={experience} value={p.experience} onChange={(v) => setP('experience', v)} />
          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Зарплата от">
              <Input type="number" min={0} value={p.salary ?? ''} onChange={(e) => setP('salary', e.target.value === '' ? null : Number(e.target.value))} />
            </Field>
            <Field label="Валюта">
              <Select value={p.currency} onChange={(e) => setP('currency', e.target.value)}>
                {currencies.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Первый прогон, дней назад" hint="Не больше 30.">
              <Input type="number" min={1} max={30} value={p.period_days ?? ''} placeholder="7" onChange={(e) => setP('period_days', e.target.value === '' ? null : Number(e.target.value))} />
            </Field>
          </div>
          <Checkbox label="Только с зарплатой" checked={p.only_with_salary} onChange={(e) => setP('only_with_salary', e.target.checked)} />
        </FormSection>

        <FormSection title="Дополнительно">
          <ChipChoice label="Метки" options={labels} value={p.label} onChange={(v) => setP('label', v)} />
          <ChipChoice label="Образование" options={options?.education ?? []} value={p.education} onChange={(v) => setP('education', v)} />
          <Field label="Профессиональные роли">
            <RefPicker label="Профессиональные роли" value={p.professional_role} onChange={(v) => setP('professional_role', v)} endpoint="/api/searches/suggest/professional_roles" placeholder="Например, программист" />
          </Field>
          <Field label="Отрасль компании">
            <Select
              value=""
              onChange={(e) => {
                const opt = options?.industry.find((i) => i.id === e.target.value)
                if (opt && !p.industry.some((i) => i.id === opt.id)) setP('industry', [...p.industry, opt])
              }}
            >
              <option value="">Добавить отрасль…</option>
              {(options?.industry ?? []).map((i) => (
                <option key={i.id} value={i.id}>
                  {i.name}
                </option>
              ))}
            </Select>
          </Field>
          {p.industry.length > 0 && (
            <div className="-mt-2 flex flex-wrap gap-1">
              {p.industry.map((i) => (
                <Tag key={i.id} onRemove={() => setP('industry', p.industry.filter((x) => x.id !== i.id))} removeLabel={`Убрать отрасль «${i.name}»`}>
                  {i.name}
                </Tag>
              ))}
            </div>
          )}
        </FormSection>

        {check.error && <ErrorNotice error={check.error} title="Проверка не удалась" />}
        {preview && (
          <section className="space-y-2 rounded-lg border border-line bg-canvas p-4" aria-label="Что найдёт поиск">
            <h3 className="text-sm font-semibold">
              Найдено: {preview.found ?? '—'} <span className="font-normal text-ink-3">· показаны первые {preview.items.length}</span>
            </h3>
            <ul className="space-y-1.5 text-sm">
              {preview.items.map((v) => (
                <li key={v.hh_id} className="min-w-0">
                  <a className="font-medium text-accent underline-offset-2 hover:underline" href={v.alternate_url ?? '#'} target="_blank" rel="noreferrer">
                    {v.name}
                  </a>
                  <span className="text-ink-2">
                    {[v.employer_name, v.area_name, v.salary_from || v.salary_to ? [v.salary_from ? `от ${money(v.salary_from, v.salary_currency)}` : '', v.salary_to ? `до ${money(v.salary_to, v.salary_currency)}` : ''].filter(Boolean).join(' ') : null, shortAgo(v.published_at)]
                      .filter(Boolean)
                      .map((x) => ` · ${x}`)
                      .join('')}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </form>
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-line bg-surface px-5 py-3 sm:px-6">
        {save.error && <span className="mr-auto text-caption text-bad">{errorMessage(save.error)}</span>}
        <Button variant="ghost" onClick={close}>
          Отмена
        </Button>
        <Button onClick={() => check.mutate()} disabled={check.isPending}>
          {check.isPending ? 'Проверяю…' : 'Проверить'}
        </Button>
        <Button type="submit" form="search-form" variant="primary" disabled={save.isPending || !form.name.trim()}>
          {save.isPending ? 'Сохраняю…' : 'Сохранить'}
        </Button>
      </div>
    </>
  )
}
