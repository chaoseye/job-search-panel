import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, MoreHorizontal, Plus, Upload, X } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { apiDelete, apiGet, apiPost, apiPut, apiText, apiUpload, errorMessage } from '../api/client'
import type { HHResumeListItem, NormalizedResume, Preferences, ProfileView, ResumeRow, SelectionProfile, SkillWeight } from '../api/types'
import TagInput from '../components/TagInput'
import { Badge } from '../components/ui/Badge'
import { Button, buttonClass } from '../components/ui/Button'
import { ToggleChip } from '../components/ui/Chip'
import { cx } from '../components/ui/cx'
import { Checkbox, Field, Input, Select, Textarea } from '../components/ui/Field'
import { Menu } from '../components/ui/Menu'
import { PageHeader } from '../components/ui/PageHeader'
import { Panel } from '../components/ui/Panel'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { Tabs } from '../components/ui/Tabs'
import { formatDateTime, relativeTime } from '../lib/format'

const WORK_FORMATS: [string, string][] = [
  ['REMOTE', 'Удалённо'],
  ['HYBRID', 'Гибрид'],
  ['ON_SITE', 'В офисе'],
  ['FIELD_WORK', 'Разъездная'],
]
const EMPLOYMENT_FORMS: [string, string][] = [
  ['FULL', 'Полная'],
  ['PART', 'Частичная'],
  ['PROJECT', 'Проект'],
  ['FLY_IN_FLY_OUT', 'Вахта'],
  ['SIDE_JOB', 'Подработка'],
]

type TabId = 'form' | 'sets' | 'resumes' | 'md'

function monthsText(m: number | null): string {
  if (m === null || m === undefined) return '—'
  const y = Math.floor(m / 12)
  const r = m % 12
  const parts: string[] = []
  if (y) parts.push(`${y} г.`)
  if (r || !y) parts.push(`${r} мес`)
  return parts.join(' ')
}

function fmtMonth(s: string | null): string {
  if (!s) return 'н. в.'
  const [y, m] = s.split('-')
  return m && y ? `${m}.${y}` : s
}

/** «Профиль и критерии»: the questionnaire that selects vacancies, extra criteria sets, the resumes behind the profile
 *  and profile.md that Claude Code reads. */
export default function ProfilePage() {
  const qc = useQueryClient()
  const profile = useQuery({ queryKey: ['profile'], queryFn: () => apiGet<ProfileView>('/api/profile') })
  const sets = useQuery({ queryKey: ['selection-profiles'], queryFn: () => apiGet<{ items: SelectionProfile[] }>('/api/selection-profiles') })
  const [tab, setTab] = useState<TabId>('form')
  const refresh = (data?: ProfileView) => {
    if (data) qc.setQueryData(['profile'], data)
    void qc.invalidateQueries({ queryKey: ['profile'] })
    void qc.invalidateQueries({ queryKey: ['profile-md'] })
  }

  const data = profile.data
  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Профиль и критерии"
        meta={data && (data.version ? `profile.md версии ${data.version}, собран ${relativeTime(data.generated_at)}` : 'profile.md ещё не собран')}
      />
      {profile.isLoading && (
        <div aria-busy="true" aria-label="Загрузка профиля" className="space-y-3">
          <Skeleton className="h-10 w-80" />
          <Skeleton className="h-64 w-full" />
        </div>
      )}
      {profile.error && <ErrorNotice error={profile.error} title="Профиль не загрузился" onRetry={() => void profile.refetch()} />}
      {data && (
        <>
          {data.warnings.length > 0 && (
            <Notice tone="warn" title="Проверьте профиль">
              <ul className="list-disc space-y-0.5 pl-4">
                {data.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </Notice>
          )}
          <Tabs
            label="Разделы профиля"
            value={tab}
            onChange={(t) => setTab(t as TabId)}
            items={[
              { id: 'form', label: 'Анкета' },
              { id: 'sets', label: 'Наборы критериев', count: sets.data?.items.length || undefined },
              { id: 'resumes', label: 'Резюме', count: data.resumes.length || undefined },
              { id: 'md', label: 'profile.md' },
            ]}
          />
          {tab === 'form' && (
            <Panel title="Анкета предпочтений" meta="Основной набор критериев отбора, факты о себе и стиль писем. После сохранения панель пересоберёт profile.md.">
              <PreferencesForm key={data.version ?? 0} initial={data.preferences} resumeSkills={data.structured?.skills ?? []} onSaved={refresh} />
            </Panel>
          )}
          {tab === 'sets' && <CriteriaSets resumeSkills={data.structured?.skills ?? []} resumes={data.resumes} />}
          {tab === 'resumes' && (
            <div className="flex flex-col gap-5">
              <ResumeSources data={data} onChange={refresh} />
              {data.structured && <ExtractedData resume={data.structured} />}
              {data.file && <FilePreview file={data.file} />}
            </div>
          )}
          {tab === 'md' && <ProfileMdPanel data={data} onChange={refresh} />}
        </>
      )}
    </div>
  )
}

// ---- resumes ------------------------------------------------------------------------------------------------

function ResumeSources({ data, onChange }: { data: ProfileView; onChange: (d?: ProfileView) => void }) {
  const [manualId, setManualId] = useState(data.manual_resume_id ?? '')
  const [hhList, setHhList] = useState<HHResumeListItem[] | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const listHh = useMutation({
    mutationFn: () => apiPost<{ items: HHResumeListItem[] }>('/api/profile/hh/list'),
    onSuccess: (r) => {
      setHhList(r.items)
      onChange()
    },
  })
  const importHh = useMutation({
    mutationFn: (resume_id: string) => apiPost<ProfileView>('/api/profile/hh/import', { resume_id }),
    onSuccess: (r) => onChange(r),
  })
  const [uploadPrimary, setUploadPrimary] = useState(true)
  const upload = useMutation({
    mutationFn: (file: File) => apiUpload<ProfileView>(`/api/profile/resumes/upload?make_primary=${uploadPrimary}`, 'file', file),
    onSuccess: (r) => {
      onChange(r)
      if (fileRef.current) fileRef.current.value = ''
    },
  })
  const makePrimary = useMutation({
    mutationFn: (id: number) => apiPut<ProfileView>(`/api/profile/resumes/${id}/primary`),
    onSuccess: (r) => onChange(r),
  })
  const publish = useMutation({
    mutationFn: ({ id, force }: { id: number; force: boolean }) => apiPost<{ ok: boolean; next_publish_at: string | null }>(`/api/profile/resumes/${id}/publish?force=${force}`),
    onSuccess: () => onChange(),
  })
  const remove = useMutation({
    mutationFn: (id: number) => apiDelete<ProfileView>(`/api/profile/resumes/${id}`),
    onSuccess: (r) => onChange(r),
  })

  const mineUnavailable = data.capabilities.resumes_mine === 'unavailable'
  const getUnavailable = data.capabilities.resume_get === 'unavailable'
  const busy = listHh.isPending || importHh.isPending

  return (
    <>
      <div className="grid gap-5 lg:grid-cols-2">
        <Panel title="Из hh.ru" meta={!data.hh_logged_in ? 'Войдите в hh.ru на странице «Настройки».' : undefined}>
          <div className="space-y-4">
            <Button disabled={!data.hh_logged_in || busy || mineUnavailable} onClick={() => listHh.mutate()} title={mineUnavailable ? 'Метод недоступен по данным зонда' : undefined}>
              {listHh.isPending ? 'Запрашиваю…' : 'Показать мои резюме'}
            </Button>
            {listHh.error && <ErrorNotice error={listHh.error} title="Список резюме не получен" />}
            {hhList && (
              <ul className="divide-y divide-rule rounded-lg border border-line">
                {hhList.length === 0 && <li className="px-3.5 py-2.5 text-caption text-ink-3">Резюме не найдены.</li>}
                {hhList.map((item) => (
                  <li key={item.id} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{item.title || item.id}</div>
                      <div className="text-caption text-ink-3">
                        {item.status && <span>{item.status} · </span>}
                        обновлено {relativeTime(item.updated_at)}
                        {item.total_views !== null && item.total_views !== undefined && <span> · просмотров {item.total_views}</span>}
                      </div>
                    </div>
                    <Button size="sm" variant="primary" disabled={busy} onClick={() => importHh.mutate(item.id)}>
                      Импортировать
                    </Button>
                  </li>
                ))}
              </ul>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault()
                if (manualId.trim()) importHh.mutate(manualId.trim())
              }}
            >
              <Field
                label="resume_id вручную"
                hint={
                  data.resumes_hint.length > 0 && !hhList
                    ? `Из адреса https://hh.ru/resume/… Известные резюме: ${data.resumes_hint.map((r) => `${r.title || r.id} (${r.id})`).join('; ')}`
                    : 'Из адреса https://hh.ru/resume/…'
                }
              >
                <div className="flex gap-2">
                  <Input value={manualId} onChange={(e) => setManualId(e.target.value)} placeholder="0123456789abcdef…" />
                  <Button type="submit" disabled={!data.hh_logged_in || busy || !manualId.trim() || getUnavailable} title={getUnavailable ? 'Метод недоступен по данным зонда' : undefined}>
                    Импортировать
                  </Button>
                </div>
              </Field>
            </form>
            {importHh.error && <ErrorNotice error={importHh.error} title="Резюме не импортировано" />}
          </div>
        </Panel>

        <Panel title="Из файла" meta="PDF, DOCX, MD или TXT до 10 МБ. Из экспорта hh.ru панель распознаёт навыки, стаж и должность.">
          <div className="space-y-4">
            <div>
              <Checkbox label="Сделать основным" checked={uploadPrimary} onChange={(e) => setUploadPrimary(e.target.checked)} />
              <p className="ml-6 mt-0.5 text-caption text-ink-3">Снимите, чтобы добавить дополнительное резюме, например под другую роль, и привязать его к набору критериев.</p>
            </div>
            <label className={cx(buttonClass('secondary'), 'cursor-pointer focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus', upload.isPending && 'pointer-events-none opacity-50')}>
              <Upload aria-hidden />
              {upload.isPending ? 'Загружаю и извлекаю текст…' : 'Выбрать файл'}
              <input
                ref={fileRef}
                type="file"
                accept=".pdf,.docx,.md,.txt"
                className="sr-only"
                disabled={upload.isPending}
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) upload.mutate(f)
                }}
              />
            </label>
            {upload.error && <ErrorNotice error={upload.error} title="Файл не загружен" />}
          </div>
        </Panel>
      </div>

      <Panel title="Сохранённые резюме" flush>
        {data.resumes.length === 0 ? (
          <EmptyState inset="panel" title="Резюме пока нет">Импортируйте резюме с hh.ru или загрузите файл.</EmptyState>
        ) : (
          <Table label="Сохранённые резюме" minWidth={720}>
            <thead>
              <tr>
                <Th>Название</Th>
                <Th>Источник</Th>
                <Th align="right">Символов</Th>
                <Th>Обновлено</Th>
                <Th>Основное</Th>
                <Th>
                  <span className="sr-only">Действия</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {data.resumes.map((r: ResumeRow) => (
                <tr key={r.id}>
                  <Td className="min-w-[14rem]">
                    <span className="font-medium text-ink">{r.title || r.file_name || r.hh_resume_id}</span>
                    {r.alternate_url && (
                      <a className="ml-2 inline-flex items-center gap-0.5 text-caption text-accent underline-offset-2 hover:underline" href={r.alternate_url} target="_blank" rel="noreferrer">
                        открыть
                        <ExternalLink aria-hidden className="size-3" />
                      </a>
                    )}
                    {r.source === 'hh' && r.next_publish_at && (
                      <div className="text-caption text-ink-3">{r.can_publish_or_update ? 'можно поднять' : `поднять можно ${relativeTime(r.next_publish_at)}`}</div>
                    )}
                  </Td>
                  <Td>
                    <Badge tone={r.source === 'hh' ? 'info' : 'neutral'}>{r.source === 'hh' ? 'hh.ru' : 'файл'}</Badge>
                  </Td>
                  <Td align="right">{r.chars.toLocaleString('ru-RU')}</Td>
                  <Td className="whitespace-nowrap tabular-nums text-ink-2">{formatDateTime(r.hh_updated_at ?? r.updated_at)}</Td>
                  <Td>
                    {r.is_primary ? (
                      <Badge tone="good">основное</Badge>
                    ) : (
                      <Button size="sm" variant="ghost" className="-ml-2.5" disabled={makePrimary.isPending} onClick={() => makePrimary.mutate(r.id)}>
                        Сделать основным
                      </Button>
                    )}
                  </Td>
                  <Td className="w-px">
                    <Menu
                      label="Действия с резюме"
                      items={[
                        ...(r.source === 'hh' && r.hh_resume_id
                          ? [
                              { key: 'refresh', label: 'Обновить из hh.ru', disabled: busy || !data.hh_logged_in, onSelect: () => importHh.mutate(r.hh_resume_id!) },
                              {
                                key: 'publish',
                                label: 'Поднять на hh.ru',
                                hint: 'не чаще раза в 4 часа; нужно подтверждение зондом, повторная попытка идёт без него',
                                disabled: publish.isPending || !data.hh_logged_in,
                                onSelect: () => publish.mutate({ id: r.id, force: publish.error !== null }),
                              },
                              'separator' as const,
                            ]
                          : []),
                        {
                          key: 'remove',
                          label: 'Удалить из панели',
                          tone: 'danger' as const,
                          disabled: remove.isPending,
                          onSelect: () => {
                            if (window.confirm('Удалить это резюме из панели?')) remove.mutate(r.id)
                          },
                        },
                      ]}
                      trigger={(p) => (
                        <Button {...p} variant="ghost" size="sm" iconOnly aria-label="Действия с резюме" title="Действия">
                          <MoreHorizontal aria-hidden />
                        </Button>
                      )}
                    />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        {(makePrimary.error || remove.error || publish.error) && (
          <ErrorNotice className="mx-4 my-3 sm:mx-5" error={makePrimary.error ?? remove.error ?? publish.error} title="Действие с резюме не выполнено" />
        )}
        {publish.data?.ok && (
          <Notice tone="good" role="status" className="mx-4 my-3 sm:mx-5">
            Резюме поднято. Следующее поднятие: {formatDateTime(publish.data.next_publish_at)}.
          </Notice>
        )}
      </Panel>
    </>
  )
}

function Collapsible({ title, meta, defaultOpen, children }: { title: ReactNode; meta?: ReactNode; defaultOpen: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <Panel
      title={title}
      meta={meta}
      actions={
        <Button variant="ghost" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? 'Свернуть' : 'Развернуть'}
        </Button>
      }
    >
      {open ? children : null}
    </Panel>
  )
}

function ExtractedData({ resume }: { resume: NormalizedResume }) {
  const rows: [string, ReactNode][] = [
    ['Должность', resume.title ?? '—'],
    ['Имя', resume.full_name ?? '—'],
    ['Регион', resume.area ?? '—'],
    ['Зарплата', resume.salary_amount ? `${resume.salary_amount.toLocaleString('ru-RU')} ${resume.salary_currency ?? ''}` : '—'],
    ['Опыт всего', monthsText(resume.total_experience_months)],
    ['Формат и занятость', [...resume.work_formats, ...resume.employment_forms].join(', ') || '—'],
    ['Переезд и командировки', `${resume.relocation ?? '—'} / ${resume.business_trips ?? '—'}`],
    ['Профессиональные роли', resume.professional_roles.join(', ') || '—'],
    ['Языки', resume.languages.join('; ') || '—'],
    ['Обновлено на hh.ru', formatDateTime(resume.updated_at)],
  ]
  return (
    <Collapsible title="Данные из резюме hh.ru" defaultOpen>
      <div className="space-y-5 text-sm">
        <dl className="grid gap-x-6 gap-y-1.5 sm:grid-cols-[12rem_minmax(0,1fr)]">
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-ink-3">{k}</dt>
              <dd className="text-ink max-sm:mb-1.5">{v}</dd>
            </div>
          ))}
        </dl>
        <div>
          <h3 className="mb-1.5 text-caption font-medium text-ink-2">Ключевые навыки</h3>
          <div className="flex flex-wrap gap-1">{resume.skills.length ? resume.skills.map((s) => <Badge key={s}>{s}</Badge>) : '—'}</div>
        </div>
        <div>
          <h3 className="mb-1.5 text-caption font-medium text-ink-2">Опыт работы</h3>
          <ul className="space-y-2">
            {resume.experience.map((e, i) => (
              <li key={i} className="rounded-lg border border-line px-3.5 py-2.5">
                <div className="font-medium">
                  {[e.company, e.position].filter(Boolean).join(' — ') || '—'}
                  <span className="ml-2 font-normal tabular-nums text-ink-3">
                    {fmtMonth(e.start)} – {fmtMonth(e.end)}
                  </span>
                </div>
                {e.description && <div className="mt-1 whitespace-pre-line text-ink-2">{e.description}</div>}
              </li>
            ))}
            {resume.experience.length === 0 && <li className="text-ink-3">—</li>}
          </ul>
        </div>
        {(resume.education_level || resume.education.length > 0) && (
          <div>
            <h3 className="mb-1.5 text-caption font-medium text-ink-2">Образование {resume.education_level ? `(${resume.education_level})` : ''}</h3>
            <ul className="list-disc space-y-0.5 pl-5">
              {resume.education.map((e, i) => (
                <li key={i}>
                  {[e.name, e.organization, e.result].filter(Boolean).join(', ')}
                  {e.year ? ` (${e.year})` : ''}
                  {e.kind !== 'primary' && <span className="ml-1 text-caption text-ink-3">[{e.kind}]</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
        {resume.about && (
          <div>
            <h3 className="mb-1.5 text-caption font-medium text-ink-2">О себе</h3>
            <div className="max-w-[72ch] whitespace-pre-line text-ink">{resume.about}</div>
          </div>
        )}
      </div>
    </Collapsible>
  )
}

function FilePreview({ file }: { file: NonNullable<ProfileView['file']> }) {
  return (
    <Collapsible title="Текст из файла" meta={`${file.name ?? 'файл'} · ${file.chars.toLocaleString('ru-RU')} символов`} defaultOpen={false}>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-canvas p-4 font-mono text-xs leading-relaxed">{file.preview}</pre>
    </Collapsible>
  )
}

// ---- criteria sets ------------------------------------------------------------------------------------------

/** Several questionnaires that differ only in vacancy selection criteria: each saved search picks one. */
function CriteriaSets({ resumeSkills, resumes }: { resumeSkills: string[]; resumes: ResumeRow[] }) {
  const qc = useQueryClient()
  const sets = useQuery({ queryKey: ['selection-profiles'], queryFn: () => apiGet<{ items: SelectionProfile[] }>('/api/selection-profiles') })
  const [active, setActive] = useState<number | null>(null)
  const [newName, setNewName] = useState('')
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['selection-profiles'] })
    void qc.invalidateQueries({ queryKey: ['searches'] })
  }
  const create = useMutation({
    mutationFn: () => apiPost<SelectionProfile>('/api/selection-profiles', { name: newName }),
    onSuccess: (row) => {
      setNewName('')
      setActive(row.id)
      invalidate()
    },
  })
  const rename = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) => apiPut<SelectionProfile>(`/api/selection-profiles/${id}`, { name }),
    onSuccess: invalidate,
  })
  const remove = useMutation({
    mutationFn: (id: number) => apiDelete(`/api/selection-profiles/${id}`),
    onSuccess: () => {
      setActive(null)
      invalidate()
    },
  })
  const bindResume = useMutation({
    mutationFn: ({ id, resume_id }: { id: number; resume_id: number }) => apiPut<SelectionProfile>(`/api/selection-profiles/${id}`, { resume_id }),
    onSuccess: () => {
      invalidate()
      void qc.invalidateQueries({ queryKey: ['profile'] })
    },
  })
  const items = sets.data?.items ?? []
  const current = items.find((s) => s.id === active) ?? null
  const error = create.error ?? rename.error ?? remove.error ?? bindResume.error
  return (
    <div className="flex flex-col gap-5">
      <Panel
        title="Дополнительные наборы критериев"
        meta="Анкета остаётся основным набором. Здесь можно завести наборы с другими стоп-словами, зарплатой, форматами, регионами и весами навыков и привязать каждый к своим поискам (страница «Поиски»)."
      >
        <div className="space-y-4">
          <p className="max-w-[80ch] text-caption text-ink-2">
            Факты о себе и стиль писем остаются общими. Если у набора выбрано своё резюме, Claude получает для его вакансий отдельный профиль (profile_set&lt;N&gt;.md), а оценка
            считает навыки и стаж из этого резюме.
          </p>
          {sets.isLoading && <Skeleton className="h-7 w-72" />}
          {sets.error && <ErrorNotice error={sets.error} title="Наборы не загрузились" onRetry={() => void sets.refetch()} />}
          {sets.data && items.length === 0 && <p className="text-caption text-ink-3">Дополнительных наборов нет: все поиски используют анкету.</p>}
          {items.length > 0 && (
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Наборы критериев">
              {items.map((s) => (
                <ToggleChip key={s.id} pressed={active === s.id} onToggle={() => setActive(active === s.id ? null : s.id)}>
                  {s.name}
                  <span className="ml-1.5 tabular-nums opacity-70" title={`поисков: ${s.searches}, вакансий: ${s.vacancies}`}>
                    {s.searches}
                  </span>
                </ToggleChip>
              ))}
            </div>
          )}
          <form
            className="flex flex-col gap-2 sm:flex-row"
            onSubmit={(e) => {
              e.preventDefault()
              if (newName.trim()) create.mutate()
            }}
          >
            <Input aria-label="Название нового набора" className="w-full sm:w-72" placeholder="Название нового набора" value={newName} onChange={(e) => setNewName(e.target.value)} />
            <Button type="submit" disabled={!newName.trim() || create.isPending}>
              <Plus aria-hidden />
              Добавить копию анкеты
            </Button>
          </form>
          {error && <ErrorNotice error={error} title="Действие с набором не выполнено" />}
        </div>
      </Panel>

      {current && (
        <Panel
          title={`Набор «${current.name}»`}
          meta={`Поисков: ${current.searches} · вакансий: ${current.vacancies}`}
          actions={
            <Button
              variant="danger"
              size="sm"
              onClick={() => {
                if (window.confirm(`Удалить набор «${current.name}»? Его поиски и вакансии перейдут на анкету.`)) remove.mutate(current.id)
              }}
            >
              Удалить набор
            </Button>
          }
        >
          <div className="space-y-6">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Название">
                <Input
                  defaultValue={current.name}
                  key={current.id}
                  onBlur={(e) => {
                    const v = e.target.value.trim()
                    if (v && v !== current.name) rename.mutate({ id: current.id, name: v })
                  }}
                />
              </Field>
              <Field label="Резюме" hint={current.profile_file ?? undefined}>
                <Select value={current.resume_id ?? 0} disabled={bindResume.isPending} onChange={(e) => bindResume.mutate({ id: current.id, resume_id: Number(e.target.value) })}>
                  <option value={0}>Основное</option>
                  {resumes
                    .filter((r) => r.source === 'file')
                    .map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.file_name || r.title || `№${r.id}`}
                        {r.is_primary ? ' (основное)' : ''}
                      </option>
                    ))}
                </Select>
              </Field>
            </div>
            <PreferencesForm
              key={`set-${current.id}-${current.updated_at ?? ''}`}
              initial={current.criteria}
              resumeSkills={resumeSkills}
              criteriaOnly
              saveLabel="Сохранить критерии"
              onSave={(p) => apiPut<SelectionProfile>(`/api/selection-profiles/${current.id}`, { criteria: p })}
              onSaved={() => invalidate()}
            />
          </div>
        </Panel>
      )}
    </div>
  )
}

// ---- the questionnaire --------------------------------------------------------------------------------------

function FormSection({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="space-y-4 border-t border-line pt-5 first:border-t-0 first:pt-0">
      <div>
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {hint && <p className="mt-0.5 text-caption text-ink-3">{hint}</p>}
      </div>
      {children}
    </section>
  )
}

function ChipGroup({ label, options, value, onToggle }: { label: string; options: [string, string][]; value: string[]; onToggle: (id: string) => void }) {
  return (
    <fieldset className="min-w-0">
      <legend className="mb-1.5 text-caption font-medium text-ink-2">{label}</legend>
      <div className="flex flex-wrap gap-1.5">
        {options.map(([id, text]) => (
          <ToggleChip key={id} pressed={value.includes(id)} onToggle={() => onToggle(id)}>
            {text}
          </ToggleChip>
        ))}
      </div>
    </fieldset>
  )
}

function PreferencesForm({
  initial,
  resumeSkills,
  onSaved,
  criteriaOnly = false,
  saveLabel = 'Сохранить и пересобрать profile.md',
  onSave,
}: {
  initial: Preferences
  resumeSkills: string[]
  onSaved: (d?: ProfileView) => void
  criteriaOnly?: boolean
  saveLabel?: string
  onSave?: (p: Preferences) => Promise<unknown>
}) {
  const [p, setP] = useState<Preferences>(initial)
  const [dirty, setDirty] = useState(false)
  useEffect(() => {
    setP(initial)
    setDirty(false)
  }, [initial])

  const set = <K extends keyof Preferences>(key: K, value: Preferences[K]) => {
    setP((prev) => ({ ...prev, [key]: value }))
    setDirty(true)
  }
  const setLetter = <K extends keyof Preferences['letter_style']>(key: K, value: Preferences['letter_style'][K]) => {
    setP((prev) => ({ ...prev, letter_style: { ...prev.letter_style, [key]: value } }))
    setDirty(true)
  }

  const [reprocessNote, setReprocessNote] = useState(false)
  const save = useMutation({
    mutationFn: () => (onSave ? onSave(p) : apiPut<ProfileView>('/api/profile/preferences', p)),
    onSuccess: (r) => {
      setDirty(false)
      setReprocessNote(Boolean(!onSave && (r as ProfileView)?.reprocess_started))
      onSaved(onSave ? undefined : (r as ProfileView))
    },
  })

  const toggle = (key: 'work_formats' | 'employment_forms', id: string) => {
    const list = p[key]
    set(key, list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  const numberOrNull = (v: string) => (v.trim() === '' ? null : Number(v))

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        if (dirty) save.mutate()
      }}
    >
      <div className="space-y-6">
        <FormSection title="Что ищу" hint="Enter или запятая добавляют слово в список.">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Целевые должности">
              <TagInput label="Целевые должности" value={p.target_titles} onChange={(v) => set('target_titles', v)} placeholder="Backend developer, Python developer" />
            </Field>
            <Field label="Ключевые слова, что ищу">
              <TagInput label="Ключевые слова, что ищу" value={p.target_keywords} onChange={(v) => set('target_keywords', v)} placeholder="python, fastapi" />
            </Field>
            <Field label="Стоп-слова в названии вакансии">
              <TagInput label="Стоп-слова в названии вакансии" value={p.stop_words_title} onChange={(v) => set('stop_words_title', v)} placeholder="1С, стажёр" />
            </Field>
            <Field label="Стоп-слова в описании">
              <TagInput label="Стоп-слова в описании" value={p.stop_words_description} onChange={(v) => set('stop_words_description', v)} placeholder="холодные звонки" />
            </Field>
          </div>
        </FormSection>

        <FormSection title="Деньги и условия">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Минимальная зарплата, на руки">
              <Input type="number" min={0} value={p.salary_min ?? ''} onChange={(e) => set('salary_min', numberOrNull(e.target.value))} />
            </Field>
            <Field label="Валюта">
              <Select value={p.salary_currency} onChange={(e) => set('salary_currency', e.target.value)}>
                {['RUR', 'USD', 'EUR', 'KZT', 'BYR'].map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Вакансии без зарплаты">
              <Select value={p.salary_missing} onChange={(e) => set('salary_missing', e.target.value as 'keep' | 'skip')}>
                <option value="keep">Оставлять</option>
                <option value="skip">Пропускать</option>
              </Select>
            </Field>
            <Field label="Командировки">
              <Select value={p.business_trips} onChange={(e) => set('business_trips', e.target.value as Preferences['business_trips'])}>
                <option value="never">Нет</option>
                <option value="rarely">Редко</option>
                <option value="ready">Готов</option>
              </Select>
            </Field>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <ChipGroup label="Формат работы" options={WORK_FORMATS} value={p.work_formats} onToggle={(id) => toggle('work_formats', id)} />
            <ChipGroup label="Занятость" options={EMPLOYMENT_FORMS} value={p.employment_forms} onToggle={(id) => toggle('employment_forms', id)} />
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            <Checkbox label="Готов к переезду" checked={p.relocation} onChange={(e) => set('relocation', e.target.checked)} />
            <Checkbox label="Без кадровых агентств" checked={p.no_agencies} onChange={(e) => set('no_agencies', e.target.checked)} />
            <Checkbox label="Только аккредитованные IT-компании" checked={p.accredited_it_only} onChange={(e) => set('accredited_it_only', e.target.checked)} />
          </div>
        </FormSection>

        <FormSection title="География, опыт и исключения">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Регионы" hint="Названия как на hh.ru: Москва, Нижний Новгород.">
              <TagInput label="Регионы" value={p.areas} onChange={(v) => set('areas', v)} placeholder="Москва" />
            </Field>
            <Field label="Языки">
              <TagInput label="Языки" value={p.languages} onChange={(v) => set('languages', v)} placeholder="Английский — B2" />
            </Field>
            <Field label="Исключённые работодатели">
              <TagInput label="Исключённые работодатели" value={p.excluded_employers} onChange={(v) => set('excluded_employers', v)} />
            </Field>
            <Field label="Исключённые отрасли">
              <TagInput label="Исключённые отрасли" value={p.excluded_industries} onChange={(v) => set('excluded_industries', v)} />
            </Field>
          </div>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Требуемый опыт в вакансии, лет: от">
              <Input type="number" min={0} step={0.5} value={p.experience_min_years ?? ''} onChange={(e) => set('experience_min_years', numberOrNull(e.target.value))} />
            </Field>
            <Field label="до">
              <Input type="number" min={0} step={0.5} value={p.experience_max_years ?? ''} onChange={(e) => set('experience_max_years', numberOrNull(e.target.value))} />
            </Field>
            {!criteriaOnly && (
              <Field label="Мой стаж, лет" hint="Сравнивается с требуемым опытом при оценке; если пусто, берётся из резюме hh.ru (файл стажа не даёт)." className="sm:col-span-2">
                <Input type="number" min={0} max={60} step={0.1} value={p.own_experience_years ?? ''} onChange={(e) => set('own_experience_years', numberOrNull(e.target.value))} />
              </Field>
            )}
          </div>
        </FormSection>

        {!criteriaOnly && (
          <FormSection title="О себе" hint="Это читает Claude, когда оценивает вакансии и пишет письма.">
            <div className="grid gap-4 md:grid-cols-3">
              <Field label="Хочу">
                <Textarea value={p.wants} onChange={(e) => set('wants', e.target.value)} placeholder="Продуктовые команды, backend на Python…" />
              </Field>
              <Field label="Не хочу">
                <Textarea value={p.avoids} onChange={(e) => set('avoids', e.target.value)} placeholder="Аутстафф, ночные смены…" />
              </Field>
              <Field label="Факты о себе вне резюме" hint="Дата выхода, гражданство, ограничения: всё, что Claude должен знать.">
                <Textarea value={p.extra_facts} onChange={(e) => set('extra_facts', e.target.value)} />
              </Field>
            </div>
          </FormSection>
        )}

        <FormSection title="Навыки с весами" hint="5 означает ключевой навык, 1 второстепенный. Веса работают в предварительной оценке вакансий.">
          <SkillsEditor value={p.skills_weights} resumeSkills={resumeSkills} onChange={(v) => set('skills_weights', v)} />
        </FormSection>

        {!criteriaOnly && (
          <FormSection title="Стиль сопроводительных писем">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Тон">
                <Input value={p.letter_style.tone} onChange={(e) => setLetter('tone', e.target.value)} />
              </Field>
              <Field label="Обращение">
                <Input value={p.letter_style.greeting} onChange={(e) => setLetter('greeting', e.target.value)} />
              </Field>
              <Field label="Длина, символов: от">
                <Input type="number" min={100} value={p.letter_style.min_chars} onChange={(e) => setLetter('min_chars', Number(e.target.value))} />
              </Field>
              <Field label="до">
                <Input type="number" min={200} max={10000} value={p.letter_style.max_chars} onChange={(e) => setLetter('max_chars', Number(e.target.value))} />
              </Field>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="Обязательно упоминать">
                <TagInput label="Обязательно упоминать" value={p.letter_style.must_mention} onChange={(v) => setLetter('must_mention', v)} />
              </Field>
              <Field label="Избегать">
                <TagInput label="Избегать" value={p.letter_style.avoid} onChange={(v) => setLetter('avoid', v)} />
              </Field>
            </div>
            <div className="space-y-2">
              <div className="text-caption font-medium text-ink-2">Примеры писем, которые нравятся (до трёх)</div>
              {p.letter_style.examples.map((ex, i) => (
                <div key={i} className="flex gap-2">
                  <Textarea
                    aria-label={`Пример письма ${i + 1}`}
                    value={ex}
                    onChange={(e) => {
                      const next = [...p.letter_style.examples]
                      next[i] = e.target.value
                      setLetter('examples', next)
                    }}
                  />
                  <Button variant="ghost" iconOnly aria-label={`Убрать пример ${i + 1}`} title="Убрать пример" onClick={() => setLetter('examples', p.letter_style.examples.filter((_, j) => j !== i))}>
                    <X aria-hidden />
                  </Button>
                </div>
              ))}
              {p.letter_style.examples.length < 3 && (
                <Button variant="ghost" className="-ml-3.5" onClick={() => setLetter('examples', [...p.letter_style.examples, ''])}>
                  <Plus aria-hidden />
                  Добавить пример
                </Button>
              )}
            </div>
          </FormSection>
        )}
      </div>

      <div className="sticky bottom-0 z-10 -mx-4 mt-6 flex flex-wrap items-center justify-end gap-3 border-t border-line bg-surface/95 px-4 py-3 backdrop-blur-sm sm:-mx-5 sm:px-5">
        {save.error && <span className="mr-auto text-caption text-bad">{errorMessage(save.error)}</span>}
        {!save.error && dirty && <span className="mr-auto text-caption font-medium text-warn">Есть несохранённые изменения</span>}
        {!save.error && !dirty && reprocessNote && (
          <span role="status" className="mr-auto text-caption text-good">
            Критерии изменились: «ниже порога» и отфильтрованные переоцениваются в фоне
          </span>
        )}
        <Button type="submit" variant="primary" disabled={save.isPending || !dirty}>
          {save.isPending ? 'Сохраняю…' : saveLabel}
        </Button>
      </div>
    </form>
  )
}

function SkillsEditor({ value, resumeSkills, onChange }: { value: SkillWeight[]; resumeSkills: string[]; onChange: (v: SkillWeight[]) => void }) {
  const [draft, setDraft] = useState('')
  const known = new Set(value.map((s) => s.name.toLowerCase()))
  const missing = resumeSkills.filter((s) => !known.has(s.toLowerCase()))
  const add = (name: string) => {
    const n = name.trim()
    if (!n || known.has(n.toLowerCase())) return
    onChange([...value, { name: n, weight: 3 }])
    setDraft('')
  }
  return (
    <div className="space-y-3">
      {value.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Навыки с весами">
          {value.map((s) => (
            <li key={s.name} className="inline-flex h-7 items-center gap-1 rounded-md border border-line bg-surface pl-2 pr-0.5 text-caption">
              <span className="max-w-[16rem] truncate">{s.name}</span>
              <select
                aria-label={`Вес навыка ${s.name}`}
                className="h-5 cursor-pointer rounded-sm bg-sunken px-1 text-xs tabular-nums text-ink outline-none focus-visible:outline-2 focus-visible:outline-focus"
                value={s.weight}
                onChange={(e) => onChange(value.map((x) => (x.name === s.name ? { ...x, weight: Number(e.target.value) } : x)))}
              >
                {[5, 4, 3, 2, 1].map((w) => (
                  <option key={w} value={w}>
                    {w}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="grid size-6 place-items-center rounded-sm text-ink-3 transition-colors hover:bg-sunken hover:text-ink"
                onClick={() => onChange(value.filter((x) => x.name !== s.name))}
                aria-label={`Удалить ${s.name}`}
                title={`Удалить ${s.name}`}
              >
                <X aria-hidden className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          aria-label="Добавить навык"
          className="sm:max-w-xs"
          placeholder="Добавить навык"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add(draft)
            }
          }}
        />
        <Button onClick={() => add(draft)} disabled={!draft.trim()}>
          Добавить
        </Button>
        {missing.length > 0 && (
          <Button variant="ghost" onClick={() => onChange([...value, ...missing.map((name) => ({ name, weight: 3 }))])}>
            Добавить из резюме ({missing.length})
          </Button>
        )}
      </div>
    </div>
  )
}

// ---- profile.md ---------------------------------------------------------------------------------------------

function ProfileMdPanel({ data, onChange }: { data: ProfileView; onChange: (d?: ProfileView) => void }) {
  const [open, setOpen] = useState(true)
  const md = useQuery({
    queryKey: ['profile-md', data.version],
    queryFn: () => apiText('/api/profile/md'),
    enabled: open && data.version !== null,
  })
  const rebuild = useMutation({
    mutationFn: () => apiPost<ProfileView>('/api/profile/rebuild'),
    onSuccess: (r) => onChange(r),
  })
  return (
    <Panel
      title="profile.md для Claude Code"
      meta={
        <span className="break-all">
          {data.profile_md_path} · {data.profile_md_exists ? 'файл на месте' : 'файла нет'} · {data.profile_md_chars.toLocaleString('ru-RU')} символов
        </span>
      }
      actions={
        <>
          <Button variant="ghost" size="sm" disabled={data.version === null} aria-expanded={open} onClick={() => setOpen(!open)}>
            {open ? 'Скрыть' : 'Показать'}
          </Button>
          <Button size="sm" disabled={rebuild.isPending || data.resumes.length === 0} onClick={() => rebuild.mutate()}>
            {rebuild.isPending ? 'Собираю…' : 'Пересобрать'}
          </Button>
        </>
      }
    >
      {rebuild.error && <ErrorNotice className="mb-3" error={rebuild.error} title="profile.md не пересобран" />}
      {data.version === null ? (
        <EmptyState inset="inline" title="profile.md ещё не собран">
          Добавьте резюме на вкладке «Резюме» и сохраните анкету.
        </EmptyState>
      ) : (
        open && (
          <pre className="max-h-[36rem] overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-canvas p-4 font-mono text-xs leading-relaxed text-ink">
            {md.isLoading ? 'Загрузка…' : md.error ? errorMessage(md.error) : md.data}
          </pre>
        )
      )}
    </Panel>
  )
}
