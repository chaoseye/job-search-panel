import { useMutation, useQuery, useQueryClient, type UseMutationResult } from '@tanstack/react-query'
import { ChevronDown, ChevronUp, Clock, Copy, ExternalLink, MoreHorizontal } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, apiPost, apiPut } from '../api/client'
import type { QuestionAnswersView, VacancyDetail } from '../api/types'
import { formatDateTime, plural, shortAgo, shortDate } from '../lib/format'
import { experienceShort, formatsShort, letterBadge, pipelineOf, rowFlags, salaryNet, salaryPublished } from '../lib/vacancyView'
import { Badge } from './ui/Badge'
import { Button, buttonClass } from './ui/Button'
import { cx } from './ui/cx'
import { Input, Textarea } from './ui/Field'
import { Menu, type MenuEntry } from './ui/Menu'
import { PipelineTrack } from './ui/PipelineTrack'
import { Popover } from './ui/Popover'
import { ScoreMeter } from './ui/Score'
import { Sheet, SheetCloseButton } from './ui/Sheet'
import { EmptyState, ErrorNotice, Notice, Skeleton } from './ui/States'
import { Tabs } from './ui/Tabs'

const VERDICT_LABELS: Record<string, string> = { apply: 'Подходит', maybe: 'Возможно', skip: 'Не подходит' }
const SALARY_FIT: Record<string, string> = { below: 'зарплата ниже ожиданий', within: 'зарплата в норме', above: 'зарплата выше ожиданий' }
const SENIORITY_FIT: Record<string, string> = { under: 'уровень ниже вашего', match: 'уровень ваш', over: 'уровень выше вашего' }
const ORIGIN_LABELS: Record<string, string> = { claude: 'Claude', edited: 'ваша правка', template: 'шаблон' }
const SNOOZE_DAYS: [number, string][] = [
  [1, 'до завтра'],
  [3, 'на 3 дня'],
  [7, 'на неделю'],
  [14, 'на 2 недели'],
  [30, 'на месяц'],
]

type TabId = 'letter' | 'evaluation' | 'details' | 'history'

export interface DrawerPosition {
  index: number
  total: number
  onPrev?: () => void
  onNext?: () => void
}

/** The card of a vacancy in a side sheet: what it is and where it stands at the top, the actions next to the title,
 *  then the letter, Claude's evaluation, the description and the history on tabs. ↑/↓ (or K/J) walk the list. */
export default function VacancyDrawer({
  id,
  onClose,
  onChanged,
  applyMode,
  onApply,
  position,
}: {
  id: number
  onClose: () => void
  onChanged: () => void
  applyMode: 'api' | 'assist'
  onApply: (id: number) => void
  position?: DrawerPosition
}) {
  return (
    <Sheet onClose={onClose} labelledBy="vacancy-title">
      <VacancyCard key={id} id={id} onChanged={onChanged} applyMode={applyMode} onApply={onApply} position={position} />
    </Sheet>
  )
}

function VacancyCard({
  id,
  onChanged,
  applyMode,
  onApply,
  position,
}: {
  id: number
  onChanged: () => void
  applyMode: 'api' | 'assist'
  onApply: (id: number) => void
  position?: DrawerPosition
}) {
  const qc = useQueryClient()
  const detail = useQuery({ queryKey: ['vacancy', id], queryFn: () => apiGet<VacancyDetail>(`/api/vacancies/${id}`) })
  const [tab, setTab] = useState<TabId | null>(null)
  const act = useMutation({
    mutationFn: ({ action, body }: { action: string; body?: unknown }) => apiPost<VacancyDetail>(`/api/vacancies/${id}/${action}`, body ?? {}),
    onSuccess: (d) => {
      qc.setQueryData(['vacancy', id], d)
      onChanged()
    },
  })
  const d = detail.data
  useEffect(() => {
    if (d && tab === null) setTab(d.letter ? 'letter' : d.evaluation ? 'evaluation' : 'details')
  }, [d, tab])

  return (
    <>
      <div className="flex h-12 shrink-0 items-center gap-1 border-b border-line px-3 sm:px-4">
        {position && position.total > 1 && (
          <>
            <Button variant="ghost" size="sm" iconOnly onClick={position.onPrev} disabled={!position.onPrev} aria-label="Предыдущая вакансия" title="Предыдущая (K)" aria-keyshortcuts="K">
              <ChevronUp aria-hidden />
            </Button>
            <Button variant="ghost" size="sm" iconOnly onClick={position.onNext} disabled={!position.onNext} aria-label="Следующая вакансия" title="Следующая (J)" aria-keyshortcuts="J">
              <ChevronDown aria-hidden />
            </Button>
            <span className="ml-1 text-caption tabular-nums text-ink-3">
              {position.index + 1} из {position.total}
            </span>
          </>
        )}
        <div className="ml-auto flex items-center gap-1">
          {d?.alternate_url && (
            <a className={buttonClass('ghost', 'sm')} href={d.alternate_url} target="_blank" rel="noreferrer">
              hh.ru
              <ExternalLink aria-hidden />
            </a>
          )}
          <SheetCloseButton />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {detail.isLoading && <CardSkeleton />}
        {detail.error && <ErrorNotice className="m-5" error={detail.error} title="Не удалось открыть вакансию" onRetry={() => void detail.refetch()} />}
        {d && (
          <>
            <CardHeader d={d} act={act} applyMode={applyMode} onApply={onApply} />
            <div className="px-4 sm:px-6">
              <Tabs
                label="Разделы карточки"
                value={tab ?? 'letter'}
                onChange={(t) => setTab(t as TabId)}
                items={[
                  { id: 'letter', label: 'Письмо' },
                  { id: 'evaluation', label: 'Оценка' },
                  { id: 'details', label: 'Описание' },
                  { id: 'history', label: 'История', count: d.history.length || undefined },
                ]}
              />
            </div>
            <div className="px-4 pb-10 pt-5 sm:px-6">
              {tab === 'letter' && <LetterTab d={d} act={act} onChanged={onChanged} />}
              {tab === 'evaluation' && <EvaluationTab d={d} act={act} />}
              {tab === 'details' && <DetailsTab d={d} />}
              {tab === 'history' && <HistoryTab d={d} />}
            </div>
          </>
        )}
      </div>
    </>
  )
}

type Act = UseMutationResult<VacancyDetail, Error, { action: string; body?: unknown }>

function CardSkeleton() {
  return (
    <div className="space-y-3 px-6 py-5">
      <Skeleton className="h-6 w-3/4" />
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-4 w-2/5" />
      <Skeleton className="mt-6 h-9 w-72" />
      <Skeleton className="mt-6 h-40 w-full" />
    </div>
  )
}

function Dot() {
  return <span aria-hidden className="text-ink-3"> · </span>
}

function CardHeader({ d, act, applyMode, onApply }: { d: VacancyDetail; act: Act; applyMode: 'api' | 'assist'; onApply: (id: number) => void }) {
  const status = d.pipeline_status
  const active = !['skipped', 'hidden', 'already_applied'].includes(status)
  // a response exists (sent, on its way, already on hh.ru): the server refuses a new packet and a return to candidates
  const responded = ['sent', 'queued', 'already_applied'].includes(status)
  const canApply = !['sent', 'queued', 'already_applied', 'hidden', 'skipped'].includes(status)
  const blockReason = d.letter?.send_problems.join('; ') || (d.response_letter_required && !d.letter ? 'работодатель требует сопроводительное письмо, а его ещё нет' : '')
  const canBatch = status !== 'awaiting_claude' && !responded
  const net = salaryNet(d.salary)
  const published = salaryPublished(d.salary)
  const flags = rowFlags(d.flags)
  const { steps, exit } = pipelineOf(d)

  const more: MenuEntry[] = []
  if (d.alternate_url) more.push({ key: 'hh', label: 'Открыть вакансию на hh.ru', href: d.alternate_url })
  if (d.apply_alternate_url) more.push({ key: 'apply-page', label: 'Страница отклика на hh.ru', href: d.apply_alternate_url })
  if (more.length) more.push('separator')
  more.push({
    key: 'reprocess',
    label: 'Переоценить',
    hint: 'загрузить описание заново и пересчитать правила; у отправленной, в пакете или с оценкой Claude обновится только описание',
    onSelect: () => act.mutate({ action: 'reprocess' }),
  })
  if (canBatch && d.evaluation) more.push({ key: 'to-batch', label: 'Переразобрать в Claude', hint: 'в следующий пакет: новая оценка и письмо', onSelect: () => act.mutate({ action: 'to-batch' }) })
  if (active) {
    more.push('separator')
    more.push({ key: 'hide', label: 'Скрыть вакансию', tone: 'danger', onSelect: () => act.mutate({ action: 'hide' }) })
    more.push({
      key: 'hide-employer',
      label: `Скрыть работодателя${d.employer.name ? ` «${d.employer.name}»` : ''}`,
      hint: 'все его вакансии уйдут в «Скрытые»',
      tone: 'danger',
      onSelect: () => {
        if (window.confirm(`Скрыть все вакансии работодателя «${d.employer.name}»?`)) act.mutate({ action: 'hide-employer' })
      },
    })
  }

  return (
    <header className="space-y-4 px-4 pb-4 pt-5 sm:px-6">
      <div>
        <h2 id="vacancy-title" className="text-lg font-semibold leading-snug text-ink">
          {d.name}
        </h2>
        <div className="mt-1 text-sm text-ink-2">
          <span className="font-medium text-ink">{d.employer.name ?? 'Работодатель не указан'}</span>
          {d.employer.accredited_it && (
            <span className="ml-1.5 align-middle">
              <Badge tone="outline" title="Аккредитованная IT-компания">
                IT
              </Badge>
            </span>
          )}
          {d.area_name && (
            <>
              <Dot />
              {d.area_name}
            </>
          )}
          {formatsShort(d) && (
            <>
              <Dot />
              {formatsShort(d)}
            </>
          )}
          {experienceShort(d.experience_name) && (
            <>
              <Dot />
              опыт {experienceShort(d.experience_name)}
            </>
          )}
          {d.employment_form_name && (
            <>
              <Dot />
              {d.employment_form_name.toLowerCase()}
            </>
          )}
        </div>
        <div className="mt-1 text-sm text-ink-2">
          {net ? (
            <span className="font-medium tabular-nums text-ink">{net} на руки</span>
          ) : (
            <span>зарплата не указана</span>
          )}
          {net && published && <span className="text-ink-3"> ({published})</span>}
          <Dot />
          <span title={formatDateTime(d.published_at)}>опубликована {shortAgo(d.published_at)}</span>
          {d.responses_count !== null && (
            <>
              <Dot />
              <span className="tabular-nums">
                {d.responses_count} {plural(d.responses_count, ['отклик', 'отклика', 'откликов'])}
              </span>
            </>
          )}
        </div>
        {(d.address || d.metro.length > 0) && (
          <div className="mt-1 text-caption text-ink-3">
            {d.address}
            {d.address && d.metro.length > 0 && ' · '}
            {d.metro.length > 0 && `м. ${d.metro.join(', ')}`}
          </div>
        )}
        {flags.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {flags.map((f) => (
              <Badge key={f.key} tone="warn">
                {f.label}
              </Badge>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <ScoreMeter
          score={d.score}
          prescore={d.prescore}
          verdict={d.evaluation ? VERDICT_LABELS[d.evaluation.verdict] : null}
          note={d.evaluation ? `Claude, ${shortDate(d.evaluation.created_at)}` : d.prescore !== null ? 'Claude ещё не разбирал' : null}
        />
        <PipelineTrack steps={steps} exit={exit} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {canApply && (
          <Button variant="primary" onClick={() => onApply(d.id)} disabled={Boolean(blockReason)}>
            {applyMode === 'api' ? 'Откликнуться через API' : 'Открыть отклик на hh.ru'}
          </Button>
        )}
        {active && <SkipButton act={act} />}
        {active && <SnoozeMenu d={d} act={act} />}
        {!active && status !== 'already_applied' && (
          <Button variant="primary" onClick={() => act.mutate({ action: 'restore' })} disabled={act.isPending}>
            Вернуть в кандидаты
          </Button>
        )}
        {(status === 'filtered_out' || status === 'not_suitable') && (
          <Button onClick={() => act.mutate({ action: 'restore' })} disabled={act.isPending} title="Правила к ней больше не применяются">
            Оставить как кандидата
          </Button>
        )}
        {canBatch && !d.evaluation && (
          <Button onClick={() => act.mutate({ action: 'to-batch' })} disabled={act.isPending} title="Включить в следующий пакет для Claude Code">
            В пакет для Claude
          </Button>
        )}
        <Menu
          label="Ещё действия"
          items={more}
          trigger={(p) => (
            <Button {...p} variant="ghost" iconOnly aria-label="Ещё действия" title="Ещё действия">
              <MoreHorizontal aria-hidden />
            </Button>
          )}
        />
      </div>

      <StatusLine d={d} blockReason={canApply ? blockReason : ''} />
      {act.error && <ErrorNotice error={act.error} title="Действие не выполнено" />}
    </header>
  )
}

/** One or two sentences on where the vacancy stands and what is in the way, said in words, not only as colours. */
function StatusLine({ d, blockReason }: { d: VacancyDetail; blockReason: string }) {
  const lines: ReactNode[] = []
  const s = d.pipeline_status
  if (s === 'sent') lines.push('Отклик отправлен.')
  if (s === 'queued') lines.push('В очереди на отправку через API.')
  if (s === 'already_applied') lines.push('На hh.ru уже есть отклик.')
  if (s === 'awaiting_claude') lines.push(`В пакете ${d.batch_id ?? ''}: Claude разберёт его при следующем запуске /hh-batch.`)
  if (d.snoozed_until) lines.push(`Отложена до ${formatDateTime(d.snoozed_until)}.`)
  if (d.pipeline_reason) lines.push(`Причина: ${d.pipeline_reason}.`)
  if (d.user_override) lines.push('Оставлена вручную: правила отбора к ней не применяются.')
  if (d.selection_profile_name) lines.push(`Критерии отбора: ${d.selection_profile_name}.`)
  if (blockReason) lines.push(`Отклик пока нельзя открыть: ${blockReason}.`)
  if (!lines.length && d.duplicate_of_id === null) return null
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-ink-2">
      {d.duplicate_of_id !== null && (
        <span>
          Повтор вакансии, которая уже есть в базе:{' '}
          <Link className="font-medium text-accent underline-offset-2 hover:underline" to={`/vacancies?open=${d.duplicate_of_id}`}>
            открыть оригинал
          </Link>
          .
        </span>
      )}
      {lines.map((l, i) => (
        <span key={i}>{l}</span>
      ))}
    </div>
  )
}

function SkipButton({ act }: { act: Act }) {
  const [reason, setReason] = useState('')
  return (
    <Popover
      label="Пропустить вакансию"
      className="w-80"
      trigger={(p) => (
        <Button {...p} disabled={act.isPending}>
          Пропустить
        </Button>
      )}
    >
      {(close) => (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault()
            act.mutate({ action: 'skip', body: { reason: reason.trim() || null } })
            setReason('')
            close()
          }}
        >
          <label className="block text-sm font-medium" htmlFor="skip-reason">
            Почему пропускаете?
          </label>
          <Input id="skip-reason" data-autofocus value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Необязательно: например, далеко ездить" />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={close}>
              Отмена
            </Button>
            <Button type="submit" variant="primary">
              Пропустить
            </Button>
          </div>
        </form>
      )}
    </Popover>
  )
}

function SnoozeMenu({ d, act }: { d: VacancyDetail; act: Act }) {
  const items: MenuEntry[] = SNOOZE_DAYS.map(([days, label]) => ({ key: `d${days}`, label: `Отложить ${label}`, onSelect: () => act.mutate({ action: 'snooze', body: { days } }) }))
  if (d.snoozed_until) items.push('separator', { key: 'unsnooze', label: 'Вернуть из отложенных', onSelect: () => act.mutate({ action: 'unsnooze' }) })
  return (
    <Menu
      label="Отложить"
      align="start"
      items={items}
      trigger={(p) => (
        <Button {...p} disabled={act.isPending}>
          <Clock aria-hidden />
          Отложить
          <ChevronDown aria-hidden className="-mr-1 text-ink-3" />
        </Button>
      )}
    />
  )
}

// ---- letter --------------------------------------------------------------------------------------------------

function LetterTab({ d, act, onChanged }: { d: VacancyDetail; act: Act; onChanged: () => void }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<string | null>(null)
  const [revision, setRevision] = useState('')
  const check = useMutation({
    mutationFn: (text: string) =>
      apiPost<{ skipped?: string; result: { score: number; band: string; hard_bans: string[]; marker_count: number } | null; warnings: string[] }>('/api/vacancies/letter-check', { text }),
  })
  const save = useMutation({
    mutationFn: (text: string) => apiPut<VacancyDetail>(`/api/vacancies/${d.id}/letter`, { text }),
    onSuccess: (data) => {
      qc.setQueryData(['vacancy', d.id], data)
      setDraft(null)
      onChanged() // the row's status follows the letter («Уточнить письмо» → «Письмо готово»)
    },
  })
  const letter = d.letter
  const text = draft ?? letter?.text ?? ''
  const dirty = draft !== null && draft !== (letter?.text ?? '')
  const badge = letterBadge(d.letter_status)

  const versions: MenuEntry[] = d.letters.map((v) => ({
    key: String(v.id),
    label: `Версия ${v.version} · ${ORIGIN_LABELS[v.origin] ?? v.origin}`,
    hint: `${v.char_count} знаков · ${formatDateTime(v.created_at)}`,
    checked: v.is_current,
    onSelect: () => !v.is_current && act.mutate({ action: `letter/${v.id}/restore` }),
  }))

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        {letter ? (
          <>
            <Badge tone={badge.tone}>{badge.label}</Badge>
            <span className="text-caption text-ink-3">
              версия {letter.version} · {ORIGIN_LABELS[letter.origin] ?? letter.origin} · <span className="tabular-nums">{text.length}</span> {plural(text.length, ['знак', 'знака', 'знаков'])}
            </span>
            {d.letters.length > 1 && (
              <Menu
                label="Версии письма"
                align="start"
                items={versions}
                trigger={(p) => (
                  <Button {...p} variant="ghost" size="sm">
                    Версии ({d.letters.length})
                    <ChevronDown aria-hidden />
                  </Button>
                )}
              />
            )}
          </>
        ) : (
          <span className="text-caption text-ink-2">Письма ещё нет: Claude напишет его при разборе. Можно написать самому или взять шаблон.</span>
        )}
        {d.revision_request && (
          <Badge tone="info">
            переработка {d.revision_request.status === 'queued' ? 'ждёт пакета' : 'в пакете'}: {d.revision_request.comment}
          </Badge>
        )}
      </div>

      {letter && letter.needs_clarification.length > 0 && (
        <Notice tone="warn" title="Уточните перед отправкой">
          <ul className="list-disc pl-4">
            {letter.needs_clarification.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </Notice>
      )}
      {letter && letter.warnings.length > 0 && (
        <Notice tone="warn" title="Замечания к письму">
          <ul className="list-disc pl-4">
            {letter.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </Notice>
      )}
      {check.data && (
        <Notice tone={check.data.result && check.data.result.score >= 70 && !check.data.result.hard_bans.length ? 'good' : 'warn'} title="Проверка стиля (humanizer-ru)">
          {check.data.skipped
            ? check.data.skipped
            : `Чистота ${check.data.result?.score}/100 (${check.data.result?.band})` +
              (check.data.result?.hard_bans.length ? `; жёсткие запреты: ${check.data.result.hard_bans.join('; ')}` : '') +
              (check.data.result?.marker_count ? `; маркеров: ${check.data.result.marker_count}` : '')}
        </Notice>
      )}
      {check.error && <ErrorNotice error={check.error} title="Стиль не проверен" />}

      <Textarea
        aria-label="Текст письма"
        className="min-h-[300px] font-ui text-prose"
        value={text}
        placeholder="Текст письма: напишите сами или нажмите «Шаблон»"
        onChange={(e) => setDraft(e.target.value)}
      />

      <div className="flex flex-wrap items-center gap-2">
        <Button variant={dirty ? 'primary' : 'secondary'} disabled={save.isPending || !dirty || !text.trim()} onClick={() => save.mutate(text)}>
          {save.isPending ? 'Сохраняю…' : 'Сохранить как новую версию'}
        </Button>
        {draft !== null && (
          <Button variant="ghost" onClick={() => setDraft(null)}>
            Отменить правку
          </Button>
        )}
        <Button variant="ghost" disabled={check.isPending || !text.trim()} onClick={() => check.mutate(text)} title="Сканер humanizer-ru: жёсткие запреты и маркеры нейросетевого текста (для русского)">
          {check.isPending ? 'Проверяю…' : 'Проверить стиль'}
        </Button>
        <Button variant="ghost" disabled={act.isPending} onClick={() => act.mutate({ action: 'letter/template' })}>
          Шаблон
        </Button>
        {letter && (
          <Popover
            label="Попросить Claude переделать письмо"
            className="w-96"
            trigger={(p) => (
              <Button {...p} variant="ghost">
                Переделать в Claude…
              </Button>
            )}
          >
            {(close) => (
              <form
                className="space-y-3"
                onSubmit={(e) => {
                  e.preventDefault()
                  if (!revision.trim()) return
                  act.mutate({ action: 'letter/revision', body: { comment: revision.trim() } })
                  setRevision('')
                  close()
                }}
              >
                <label className="block text-sm font-medium" htmlFor="revision-comment">
                  Что изменить?
                </label>
                <Input id="revision-comment" data-autofocus value={revision} onChange={(e) => setRevision(e.target.value)} placeholder="Короче; акцент на опыте с PostgreSQL" />
                <p className="text-caption text-ink-3">Просьба уйдёт в следующий пакет вместе с текущим текстом; новая версия появится после разбора.</p>
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" onClick={close}>
                    Отмена
                  </Button>
                  <Button type="submit" variant="primary" disabled={!revision.trim()}>
                    Отправить в пакет
                  </Button>
                </div>
              </form>
            )}
          </Popover>
        )}
      </div>
      {save.error && <ErrorNotice error={save.error} title="Письмо не сохранено" />}

      <QuestionsBlock d={d} />
    </div>
  )
}

function QuestionsBlock({ d }: { d: VacancyDetail }) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(d.question_answers.length > 0)
  const [text, setText] = useState('')
  const [copied, setCopied] = useState<string | null>(null)
  const ask = useMutation({
    mutationFn: () => apiPost<{ request: QuestionAnswersView; batch_id: string | null; answers: QuestionAnswersView[] }>(`/api/vacancies/${d.id}/questions`, { text }),
    onSuccess: () => {
      setText('')
      void qc.invalidateQueries({ queryKey: ['vacancy', d.id] })
      void qc.invalidateQueries({ queryKey: ['batches'] })
      void qc.invalidateQueries({ queryKey: ['batches-summary'] })
    },
  })
  const copy = async (key: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(key)
    } catch {
      setCopied(null)
    }
  }
  return (
    <section className="border-t border-line pt-4">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 rounded text-left text-sm font-medium text-ink focus-visible:outline-2 focus-visible:outline-focus"
      >
        <ChevronDown aria-hidden className={cx('size-4 text-ink-3 transition-transform duration-150', !open && '-rotate-90')} />
        Вопросы работодателя при отклике
        {d.question_answers.length > 0 && <span className="font-normal text-ink-3">· {d.question_answers[0].status_label}</span>}
      </button>
      {open && (
        <div className="mt-3 space-y-3">
          <p className="text-caption text-ink-2">
            Если форма отклика на hh.ru задаёт вопросы, вставьте их сюда как есть: по вопросу на абзац, варианты ответа списком. Claude ответит по фактам
            профиля с учётом письма; где фактов нет, поставит «уточнить».
          </p>
          <Textarea aria-label="Вопросы работодателя" value={text} onChange={(e) => setText(e.target.value)} placeholder={'Есть ли у вас опыт…?\n\nКакие технологии использовали?\n* Docker\n* Kafka'} />
          <div className="flex flex-wrap items-center gap-2">
            <Button disabled={ask.isPending || text.trim().length < 5} onClick={() => ask.mutate()}>
              Запросить ответы
            </Button>
            {ask.data && (
              <span className="text-caption text-good">
                {ask.data.batch_id ? `Задача в пакете ${ask.data.batch_id} — запустите /hh-batch.` : 'Задача в очереди: попадёт в следующий пакет.'}
              </span>
            )}
          </div>
          {ask.error && <ErrorNotice error={ask.error} title="Вопросы не отправлены" />}
          {d.question_answers.map((qa) => (
            <div key={qa.id} className="rounded-lg border border-line p-3">
              <div className="mb-2 flex flex-wrap items-center gap-2 text-caption text-ink-3">
                <Badge tone={qa.status === 'done' ? 'good' : qa.status === 'cancelled' ? 'bad' : 'neutral'}>{qa.status_label}</Badge>
                <span>
                  {qa.questions.length} {plural(qa.questions.length, ['вопрос', 'вопроса', 'вопросов'])} · {formatDateTime(qa.created_at)}
                </span>
                {qa.error && <span className="text-bad">{qa.error}</span>}
              </div>
              {qa.result ? (
                <ol className="list-decimal space-y-3 pl-5 text-sm">
                  {qa.result.answers.map((a, i) => (
                    <li key={i}>
                      <div className="font-medium">{a.question}</div>
                      <div className="whitespace-pre-wrap text-ink-2">{a.answer}</div>
                      {a.needs_clarification.length > 0 && <div className="text-caption text-warn">уточнить: {a.needs_clarification.join('; ')}</div>}
                      <Button variant="ghost" size="sm" className="-ml-2.5 mt-1" onClick={() => void copy(`${qa.id}-${i}`, a.answer)}>
                        <Copy aria-hidden />
                        {copied === `${qa.id}-${i}` ? 'Скопировано' : 'Скопировать'}
                      </Button>
                    </li>
                  ))}
                </ol>
              ) : (
                <ul className="list-disc space-y-1 pl-5 text-caption text-ink-2">
                  {qa.questions.map((q, i) => (
                    <li key={i} className="whitespace-pre-wrap">
                      {q}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ---- evaluation ----------------------------------------------------------------------------------------------

function EvaluationTab({ d, act }: { d: VacancyDetail; act: Act }) {
  const e = d.evaluation
  const [comment, setComment] = useState('')
  return (
    <div className="space-y-6">
      {e ? (
        <section className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-caption text-ink-3">
            {e.salary_fit && e.salary_fit !== 'unknown' && <Badge>{SALARY_FIT[e.salary_fit] ?? e.salary_fit}</Badge>}
            {e.seniority_fit && e.seniority_fit !== 'unknown' && <Badge>{SENIORITY_FIT[e.seniority_fit] ?? e.seniority_fit}</Badge>}
            {e.stale_profile && <Badge tone="warn">оценено старой версией профиля</Badge>}
            <span>{formatDateTime(e.created_at)}</span>
          </div>
          <p className="text-prose text-ink">{e.summary}</p>
          <EvalList title="Совпадает" tone="good" items={e.matched} />
          <EvalList title="Пробелы" tone="warn" items={e.gaps} />
          <EvalList title="Риски" tone="bad" items={e.red_flags} />
          <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4 text-caption">
            <span className="text-ink-2">Оценка верна?</span>
            <Button size="sm" variant={e.user_feedback === 'ok' ? 'primary' : 'secondary'} onClick={() => act.mutate({ action: 'feedback', body: { feedback: 'ok' } })} disabled={act.isPending}>
              Да
            </Button>
            <Popover
              label="Что не так с оценкой"
              className="w-96"
              trigger={(p) => (
                <Button {...p} size="sm" variant={e.user_feedback === 'wrong' ? 'primary' : 'secondary'} disabled={act.isPending}>
                  Нет…
                </Button>
              )}
            >
              {(close) => (
                <form
                  className="space-y-3"
                  onSubmit={(ev) => {
                    ev.preventDefault()
                    act.mutate({ action: 'feedback', body: { feedback: 'wrong', comment } })
                    close()
                  }}
                >
                  <label className="block text-sm font-medium" htmlFor="feedback-comment">
                    Что не так?
                  </label>
                  <Input id="feedback-comment" data-autofocus value={comment} onChange={(ev) => setComment(ev.target.value)} placeholder="Например: стек не мой, это продажи" />
                  <p className="text-caption text-ink-3">Замечание уйдёт Claude в следующих пакетах.</p>
                  <div className="flex justify-end gap-2">
                    <Button variant="ghost" onClick={close}>
                      Отмена
                    </Button>
                    <Button type="submit" variant="primary">
                      Отправить
                    </Button>
                  </div>
                </form>
              )}
            </Popover>
            {e.user_feedback === 'wrong' && e.user_comment && <span className="text-ink-3">ваше замечание: {e.user_comment}</span>}
          </div>
        </section>
      ) : (
        <EmptyState inset="inline" title="Claude ещё не оценивал эту вакансию">
          {d.pipeline_status === 'awaiting_claude'
            ? 'Она уже в пакете: оценка и письмо появятся после разбора.'
            : 'Отправьте её в пакет кнопкой «В пакет для Claude»: оценка и письмо появятся после разбора.'}
        </EmptyState>
      )}

      <section className="grid gap-4 border-t border-line pt-4 sm:grid-cols-2">
        <div>
          <h3 className="mb-1.5 text-sm font-medium">Правила отбора</h3>
          {d.rules_reasons.length ? (
            <ul className="list-disc space-y-1 pl-4 text-caption text-bad">
              {d.rules_reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          ) : (
            <p className="text-caption text-ink-2">Все правила пройдены.</p>
          )}
        </div>
        <div>
          <h3 className="mb-1.5 text-sm font-medium">Предварительная оценка{d.prescore !== null ? `: ${d.prescore}` : ''}</h3>
          {d.prescore_notes.length ? (
            <ul className="list-disc space-y-1 pl-4 text-caption text-ink-2">
              {d.prescore_notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : (
            <p className="text-caption text-ink-2">Не рассчитана.</p>
          )}
        </div>
      </section>

      {(d.tags.domain.length > 0 || d.tags.tasks.length > 0 || d.tags.stack.length > 0) && (
        <section className="space-y-2 border-t border-line pt-4">
          <h3 className="text-sm font-medium">Метки для аналитики</h3>
          <TagRow title="Отрасль" items={d.tags.domain} />
          <TagRow title="Задачи" items={d.tags.tasks} />
          <TagRow title="Инструменты" items={d.tags.stack} />
        </section>
      )}

      {d.sources.length > 0 && (
        <section className="border-t border-line pt-4 text-caption text-ink-2">
          <h3 className="mb-1.5 text-sm font-medium text-ink">Нашли поиски</h3>
          {d.sources.join(' · ')}
        </section>
      )}
    </div>
  )
}

function EvalList({ title, tone, items }: { title: string; tone: 'good' | 'warn' | 'bad'; items: string[] }) {
  if (!items.length) return null
  const dot = { good: 'bg-good', warn: 'bg-warn', bad: 'bg-bad' }[tone]
  return (
    <div>
      <h3 className="mb-1.5 flex items-center gap-2 text-sm font-medium">
        <span aria-hidden className={cx('size-2 rounded-full', dot)} />
        {title}
      </h3>
      <ul className="space-y-1 pl-4 text-sm text-ink-2">
        {items.map((i) => (
          <li key={i} className="list-disc">
            {i}
          </li>
        ))}
      </ul>
    </div>
  )
}

function TagRow({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="w-24 shrink-0 text-caption text-ink-3">{title}</span>
      {items.map((t) => (
        <Badge key={t}>{t}</Badge>
      ))}
    </div>
  )
}

// ---- description and history ---------------------------------------------------------------------------------

function DetailsTab({ d }: { d: VacancyDetail }) {
  return (
    <div className="space-y-6">
      {d.key_skills.length > 0 && (
        <section>
          <h3 className="mb-2 text-sm font-medium">
            Ключевые навыки <span className="font-normal text-ink-3">· зелёные есть в вашем профиле</span>
          </h3>
          <div className="flex flex-wrap gap-1.5">
            {d.key_skills.map((k) => (
              <Badge key={k.name} tone={k.matched ? 'good' : 'neutral'}>
                {k.name}
              </Badge>
            ))}
          </div>
        </section>
      )}
      <section>
        <h3 className="mb-2 text-sm font-medium">Описание</h3>
        {d.description_html ? (
          <div
            className="max-w-[68ch] text-prose text-ink [&_a]:text-accent [&_a]:underline [&_h3]:mt-4 [&_h3]:font-semibold [&_li]:my-1 [&_li]:ml-5 [&_li]:list-disc [&_ol>li]:list-decimal [&_p]:my-2.5 [&_strong]:font-semibold"
            dangerouslySetInnerHTML={{ __html: d.description_html }}
          />
        ) : (
          <p className="text-caption text-ink-2">Описание ещё не загружено: панель загружает его только для вакансий, прошедших правила.</p>
        )}
      </section>
    </div>
  )
}

function HistoryTab({ d }: { d: VacancyDetail }) {
  if (!d.history.length) return <EmptyState inset="inline" title="Истории пока нет" />
  return (
    <ol className="space-y-2 text-sm">
      {d.history.map((h, i) => (
        <li key={i} className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-3">
          <span className="text-caption tabular-nums text-ink-3">{formatDateTime(h.ts)}</span>
          <span className="text-ink-2">{h.message}</span>
        </li>
      ))}
    </ol>
  )
}

