import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, ExternalLink, RefreshCw, Trash2 } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { apiDelete, apiGet, apiPost } from '../api/client'
import type { ApplicationRow, InterviewPrepView, NegotiationDetail, ReplyDraftView } from '../api/types'
import { formatDateTime, plural, relativeTime } from '../lib/format'
import { Badge, type BadgeTone } from './ui/Badge'
import { Button, buttonClass } from './ui/Button'
import { cx } from './ui/cx'
import { Checkbox, Input, Select, Textarea } from './ui/Field'
import { Segmented } from './ui/Segmented'
import { Sheet, SheetCloseButton } from './ui/Sheet'
import { EmptyState, ErrorNotice, Notice } from './ui/States'
import { Tabs } from './ui/Tabs'

/** The hh.ru state of a response as a badge. */
export const STATE_TONE: Record<string, BadgeTone> = {
  response: 'neutral',
  invitation: 'good',
  discard: 'bad',
  hidden: 'outline',
}

const MANUAL_STATES: { id: string; label: string }[] = [
  { id: '', label: 'Статус без изменений' },
  { id: 'response', label: 'Отклик, ответа нет' },
  { id: 'invitation', label: 'Приглашение' },
  { id: 'discard', label: 'Отказ' },
]

const TASK_TONE: Record<string, BadgeTone> = { done: 'good', cancelled: 'bad' }

type TabId = 'chat' | 'prep' | 'letter' | 'history'

/** Side sheet with the hh.ru negotiation of a response (F-8.9.4/8.9.5): its state and dates, the messages (read only,
 *  or a manual journal when hh.ru does not give them to the app), a reply draft and an interview prep from Claude, the
 *  letter that went out and the history. */
export default function NegotiationDrawer({ application: initial, onClose }: { application: ApplicationRow; onClose: () => void }) {
  const qc = useQueryClient()
  // the manual journal attaches a negotiation to the application on first use: keep the fresh row locally
  const [application, setApplication] = useState<ApplicationRow>(initial)
  const [tab, setTab] = useState<TabId>('chat')
  const nid = application.hh_negotiation_id
  const detail = useQuery({
    queryKey: ['negotiation', nid],
    queryFn: () => apiGet<NegotiationDetail>(`/api/negotiations/${nid}`),
    enabled: Boolean(nid) && Boolean(application.negotiation),
  })
  const deleteJournal = useMutation({
    mutationFn: () => apiDelete<{ application: ApplicationRow }>(`/api/applications/${application.id}/manual-journal`),
    onSuccess: (data) => {
      setApplication(data.application)
      void qc.invalidateQueries({ queryKey: ['applications'] })
      void qc.invalidateQueries({ queryKey: ['stats'] })
    },
  })
  const neg = detail.data?.negotiation ?? application.negotiation
  const v = application.vacancy
  const manual = Boolean(neg?.manual)
  const events = detail.data?.events ?? []

  const tabs: { id: TabId; label: string; count?: number }[] = [{ id: 'chat', label: 'Переписка' }]
  if (application.status === 'sent') tabs.push({ id: 'prep', label: 'Собеседование' })
  if (application.letter_text) tabs.push({ id: 'letter', label: 'Письмо' })
  if (events.length) tabs.push({ id: 'history', label: 'История', count: events.length })

  return (
    <Sheet onClose={onClose} labelledBy="negotiation-title">
      <div className="flex h-12 shrink-0 items-center gap-1 border-b border-line px-3 sm:px-4">
        <span className="text-caption text-ink-3">Отклик</span>
        <div className="ml-auto flex items-center gap-1">
          {neg?.page_url && (
            <a className={buttonClass('ghost', 'sm')} href={neg.page_url} target="_blank" rel="noreferrer">
              hh.ru
              <ExternalLink aria-hidden />
            </a>
          )}
          <SheetCloseButton />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <header className="space-y-3 px-4 pb-4 pt-5 sm:px-6">
          <div>
            <h2 id="negotiation-title" className="text-lg font-semibold leading-snug text-ink">
              {v?.name ?? `Вакансия №${application.vacancy_id}`}
            </h2>
            <div className="mt-1 text-sm text-ink-2">
              <span className="font-medium text-ink">{v?.employer_name ?? 'Работодатель не указан'}</span>
              <span className="text-ink-3"> · </span>
              {application.mode_label}
              {application.sent_at && (
                <>
                  <span className="text-ink-3"> · </span>
                  отправлен {formatDateTime(application.sent_at)}
                </>
              )}
            </div>
          </div>
          {neg && (
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone={STATE_TONE[neg.state_id ?? 'response'] ?? 'neutral'}>{neg.state_label}</Badge>
              {neg.viewed_by_opponent && <Badge tone="info">просмотрен работодателем</Badge>}
              {neg.has_updates && <Badge tone="warn">есть обновления</Badge>}
              {neg.unread_count > 0 && <Badge tone="warn">непрочитанных: {neg.unread_count}</Badge>}
              {manual && <Badge tone="outline">ручной журнал</Badge>}
              {manual && (
                <Button
                  variant="danger"
                  size="sm"
                  disabled={deleteJournal.isPending}
                  onClick={() => {
                    if (window.confirm('Удалить весь ручной журнал по этому отклику (статусы, сообщения, черновики)? Сам отклик останется.')) deleteJournal.mutate()
                  }}
                >
                  <Trash2 aria-hidden />
                  Удалить журнал
                </Button>
              )}
            </div>
          )}
          {deleteJournal.error && <ErrorNotice error={deleteJournal.error} title="Журнал не удалён" />}
        </header>

        {tabs.length > 1 && (
          <div className="px-4 sm:px-6">
            <Tabs label="Разделы отклика" value={tab} onChange={(t) => setTab(t as TabId)} items={tabs} />
          </div>
        )}
        <div className="px-4 pb-10 pt-5 sm:px-6">
          {tab === 'chat' && <ChatTab application={application} onApplication={setApplication} detail={detail.data} />}
          {tab === 'prep' && <PrepTab application={application} />}
          {tab === 'letter' && application.letter_text && <div className="whitespace-pre-wrap rounded-lg border border-line bg-canvas p-4 text-prose text-ink">{application.letter_text}</div>}
          {tab === 'history' && (
            <ol className="space-y-2 text-sm">
              {events.map((e) => (
                <li key={e.id} className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-3">
                  <span className="text-caption tabular-nums text-ink-3">{formatDateTime(e.occurred_at)}</span>
                  <span className="text-ink-2">
                    {e.label}
                    {e.event_type === 'state_changed' && ` → ${e.to_state_label}`}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </Sheet>
  )
}

function Section({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="space-y-3 border-t border-line pt-5 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  )
}

function ChatTab({ application, onApplication, detail }: { application: ApplicationRow; onApplication: (a: ApplicationRow) => void; detail: NegotiationDetail | undefined }) {
  const qc = useQueryClient()
  const nid = application.hh_negotiation_id
  const neg = detail?.negotiation ?? application.negotiation
  const manual = Boolean(neg?.manual)
  const canRecord = application.status === 'sent' && (!neg || manual)

  const [mState, setMState] = useState('')
  const [mViewed, setMViewed] = useState(false)
  const [mText, setMText] = useState('')
  const [mAuthor, setMAuthor] = useState<'employer' | 'applicant'>('employer')
  const manualUpdate = useMutation({
    mutationFn: () =>
      apiPost<{ application: ApplicationRow; detail: NegotiationDetail }>(`/api/applications/${application.id}/manual-update`, {
        state: mState || null,
        viewed: mViewed || null,
        text: mText.trim() || null,
        author: mAuthor,
      }),
    onSuccess: (data) => {
      onApplication(data.application)
      qc.setQueryData(['negotiation', data.application.hh_negotiation_id], data.detail)
      setMState('')
      setMViewed(false)
      setMText('')
      void qc.invalidateQueries({ queryKey: ['applications'] })
      void qc.invalidateQueries({ queryKey: ['stats'] })
    },
  })
  const deleteMessage = useMutation({
    mutationFn: (messageId: number) => apiDelete<NegotiationDetail>(`/api/negotiations/${nid}/messages/${messageId}`),
    onSuccess: (data) => {
      qc.setQueryData(['negotiation', nid], data)
      void qc.invalidateQueries({ queryKey: ['applications'] })
    },
  })
  const refresh = useMutation({
    mutationFn: () => apiPost<NegotiationDetail>(`/api/negotiations/${nid}/refresh-messages`),
    onSuccess: (data) => {
      qc.setQueryData(['negotiation', nid], data)
      void qc.invalidateQueries({ queryKey: ['applications'] })
    },
  })
  const [intent, setIntent] = useState('')
  const [copied, setCopied] = useState<number | null>(null)
  const draftReq = useMutation({
    mutationFn: () => apiPost<{ draft: ReplyDraftView; batch_id: string | null }>(`/api/negotiations/${nid}/reply-draft`, { user_intent: intent || null }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['negotiation', nid] })
      void qc.invalidateQueries({ queryKey: ['batches'] })
      void qc.invalidateQueries({ queryKey: ['batches-summary'] })
    },
  })
  const copyDraft = async (d: ReplyDraftView) => {
    try {
      await navigator.clipboard.writeText(d.text ?? '')
      setCopied(d.id)
    } catch {
      setCopied(null)
    }
  }

  return (
    <div className="space-y-6">
      {!neg && !canRecord && <Notice title="Данных hh.ru по этому отклику ещё нет">Они появятся после синхронизации: она идёт каждые несколько минут, или нажмите «Синхронизировать» на странице откликов.</Notice>}

      {neg && (
        <dl className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-caption">
          <Row label="Создан на hh.ru">{formatDateTime(neg.hh_created_at)}</Row>
          <Row label="Обновлён">{formatDateTime(neg.hh_updated_at)}</Row>
          <Row label="Просмотрен">{neg.first_viewed_at ? formatDateTime(neg.first_viewed_at) : '—'}</Row>
          <Row label="Первая реакция">{neg.first_reaction_at ? formatDateTime(neg.first_reaction_at) : '—'}</Row>
          {neg.invited_at && <Row label="Приглашение">{formatDateTime(neg.invited_at)}</Row>}
          {neg.discarded_at && <Row label="Отказ">{formatDateTime(neg.discarded_at)}</Row>}
          <Row label="Переписка">{manual ? 'записывается вручную' : (neg.messaging_status ?? '—')}</Row>
          <Row label={manual ? 'Последняя запись' : 'Синхронизирован'}>{relativeTime(neg.last_synced_at)}</Row>
        </dl>
      )}

      {canRecord && (
        <Section title="Ручной журнал: что ответил работодатель">
          <p className="text-caption text-ink-2">
            hh.ru не даёт вашему приложению статусы и переписку, поэтому запишите здесь то, что видите на hh.ru: просмотр, приглашение или отказ, вопросы работодателя. Записи попадут в фильтры,
            статистику и в черновик ответа для Claude.
          </p>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
            <Select aria-label="Статус отклика" wrapperClassName="w-auto min-w-56" value={mState} onChange={(e) => setMState(e.target.value)}>
              {MANUAL_STATES.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </Select>
            <Checkbox label="Просмотрен работодателем" checked={mViewed} onChange={(e) => setMViewed(e.target.checked)} />
          </div>
          <Textarea aria-label="Текст сообщения" placeholder="Текст сообщения: вопросы, приглашение. Вставьте как есть" value={mText} onChange={(e) => setMText(e.target.value)} />
          <div className="flex flex-wrap items-center gap-3">
            <Segmented
              label="Чьё сообщение"
              options={[
                ['employer', 'Сообщение работодателя'],
                ['applicant', 'Мой ответ'],
              ]}
              value={mAuthor}
              onChange={setMAuthor}
            />
            <Button variant="primary" className="ml-auto" disabled={manualUpdate.isPending || (!mState && !mViewed && !mText.trim())} onClick={() => manualUpdate.mutate()}>
              {manualUpdate.isPending ? 'Записываю…' : 'Записать'}
            </Button>
          </div>
          {manualUpdate.error && <ErrorNotice error={manualUpdate.error} title="Запись не сохранена" />}
        </Section>
      )}

      {neg && (
        <Section
          title="Сообщения"
          action={
            !manual && (
              <Button variant="ghost" size="sm" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
                <RefreshCw aria-hidden className={cx(refresh.isPending && 'animate-spin motion-reduce:animate-none')} />
                {refresh.isPending ? 'Обновляю…' : 'Обновить переписку'}
              </Button>
            )
          }
        >
          {refresh.error && <ErrorNotice error={refresh.error} title="Переписка не обновилась" />}
          {detail && detail.messages.length === 0 && (
            <p className="text-caption text-ink-3">
              Сообщений нет{!manual && neg.messages_count > 0 ? `: hh.ru сообщает о ${neg.messages_count}, нажмите «Обновить переписку»` : ''}.
            </p>
          )}
          <ul className="space-y-2">
            {(detail?.messages ?? []).map((m) => {
              const mine = m.author_type === 'applicant'
              return (
                <li key={m.id} className={cx('max-w-[88%] rounded-lg px-3.5 py-2.5 text-sm', mine ? 'ml-auto bg-accent-soft' : 'bg-sunken')}>
                  <div className="mb-1 flex items-center gap-2 text-xs text-ink-3">
                    <span>
                      {m.author_type === 'employer' ? m.author_name || 'Работодатель' : mine ? 'Вы' : m.author_name || m.author_type} · {formatDateTime(m.created_at)}
                    </span>
                    {m.source === 'manual' && (
                      <button
                        type="button"
                        className="ml-auto rounded text-bad hover:underline"
                        onClick={() => {
                          if (window.confirm('Удалить эту запись?')) deleteMessage.mutate(m.id)
                        }}
                      >
                        удалить
                      </button>
                    )}
                  </div>
                  <div className="whitespace-pre-wrap text-ink">{m.text ?? '—'}</div>
                </li>
              )
            })}
          </ul>
          <p className="text-caption text-ink-3">
            {manual ? 'Панель ничего не отправляет на hh.ru: отвечайте там, а свой ответ при желании запишите сюда как «Мой ответ».' : 'Панель только читает переписку, отвечать нужно на hh.ru.'}
          </p>
        </Section>
      )}

      {neg && (
        <Section title="Черновик ответа через Claude">
          <p className="text-caption text-ink-2">Панель положит переписку в следующий пакет для /hh-batch, после разбора черновик появится здесь. Отправлять ответ нужно самому на hh.ru.</p>
          <form
            className="flex flex-col gap-2 sm:flex-row"
            onSubmit={(e) => {
              e.preventDefault()
              draftReq.mutate()
            }}
          >
            <Input aria-label="Что ответить" placeholder="Что ответить: например, предложить созвон завтра в 11:00" value={intent} onChange={(e) => setIntent(e.target.value)} />
            <Button type="submit" disabled={draftReq.isPending}>
              Запросить черновик
            </Button>
          </form>
          {draftReq.data && (
            <p className="text-caption text-good">{draftReq.data.batch_id ? `Задача в пакете ${draftReq.data.batch_id} — запустите /hh-batch.` : 'Задача в очереди: попадёт в следующий пакет.'}</p>
          )}
          {draftReq.error && <ErrorNotice error={draftReq.error} title="Черновик не запрошен" />}
          <ul className="space-y-2">
            {(detail?.reply_drafts ?? []).map((d) => (
              <li key={d.id} className="rounded-lg border border-line p-3.5 text-sm">
                <div className="mb-1.5 flex flex-wrap items-center gap-2 text-xs text-ink-3">
                  <Badge tone={TASK_TONE[d.status] ?? 'neutral'}>{d.status_label}</Badge>
                  {d.intent && <span>{d.intent}</span>}
                  <span>{formatDateTime(d.created_at)}</span>
                  {d.text && (
                    <Button variant="ghost" size="sm" className="ml-auto" onClick={() => void copyDraft(d)}>
                      <Copy aria-hidden />
                      {copied === d.id ? 'Скопировано' : 'Скопировать'}
                    </Button>
                  )}
                </div>
                {d.user_intent && <div className="text-caption text-ink-3">запрос: {d.user_intent}</div>}
                {d.text && <div className="whitespace-pre-wrap text-ink">{d.text}</div>}
                {d.needs_clarification.length > 0 && <div className="mt-1 text-caption text-warn">уточнить: {d.needs_clarification.join('; ')}</div>}
                {d.error && <div className="text-caption text-bad">{d.error}</div>}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-ink-3">{label}</dt>
      <dd className="tabular-nums text-ink">{children}</dd>
    </>
  )
}

function PrepTab({ application }: { application: ApplicationRow }) {
  const qc = useQueryClient()
  const preps = useQuery({
    queryKey: ['interview-preps', application.id],
    queryFn: () => apiGet<InterviewPrepView[]>(`/api/applications/${application.id}/interview-preps`),
    enabled: application.status === 'sent',
  })
  const [notes, setNotes] = useState('')
  const prepReq = useMutation({
    mutationFn: () => apiPost<{ prep: InterviewPrepView | null; batch_id: string | null; preps: InterviewPrepView[] }>(`/api/applications/${application.id}/interview-prep`, { notes: notes || null }),
    onSuccess: (data) => {
      qc.setQueryData(['interview-preps', application.id], data.preps)
      setNotes('')
      void qc.invalidateQueries({ queryKey: ['batches'] })
      void qc.invalidateQueries({ queryKey: ['batches-summary'] })
    },
  })
  return (
    <div className="space-y-4">
      <p className="text-caption text-ink-2">
        Лист подготовки по вакансии, вашему письму и переписке: вероятные вопросы с ответами из профиля, что спросить у них, риски, что освежить. Панель заказывает его сама, когда записано
        приглашение; здесь можно заказать вручную, с пометкой.
      </p>
      <form
        className="flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault()
          prepReq.mutate()
        }}
      >
        <Input aria-label="Что учесть" placeholder="Что учесть: например, будут спрашивать про Kafka" value={notes} onChange={(e) => setNotes(e.target.value)} />
        <Button type="submit" disabled={prepReq.isPending}>
          Запросить подготовку
        </Button>
      </form>
      {prepReq.data && <p className="text-caption text-good">{prepReq.data.batch_id ? `Задача в пакете ${prepReq.data.batch_id} — запустите /hh-batch.` : 'Задача в очереди: попадёт в следующий пакет.'}</p>}
      {prepReq.error && <ErrorNotice error={prepReq.error} title="Подготовка не запрошена" />}
      {preps.error && <ErrorNotice error={preps.error} title="Не удалось загрузить подготовку" onRetry={() => void preps.refetch()} />}
      {preps.data && preps.data.length === 0 && <EmptyState inset="inline" title="Подготовки пока нет" />}
      <ul className="space-y-3">
        {(preps.data ?? []).map((p) => (
          <li key={p.id} className="rounded-lg border border-line p-4 text-sm">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-ink-3">
              <Badge tone={TASK_TONE[p.status] ?? 'neutral'}>{p.status_label}</Badge>
              {p.auto && <span>по приглашению</span>}
              <span>{formatDateTime(p.created_at)}</span>
            </div>
            {p.user_notes && <div className="mb-2 text-caption text-ink-3">пометка: {p.user_notes}</div>}
            {p.result && (
              <div className="space-y-4">
                {p.result.summary && <p className="text-prose text-ink">{p.result.summary}</p>}
                {p.result.questions.length > 0 && (
                  <div>
                    <h4 className="mb-1.5 font-medium">
                      Вероятные вопросы <span className="font-normal text-ink-3">· {p.result.questions.length} {plural(p.result.questions.length, ['вопрос', 'вопроса', 'вопросов'])}</span>
                    </h4>
                    <ol className="list-decimal space-y-2 pl-5">
                      {p.result.questions.map((q, i) => (
                        <li key={i}>
                          <div className="font-medium">{q.question}</div>
                          {q.answer && <div className="whitespace-pre-wrap text-ink-2">{q.answer}</div>}
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
                <PrepList title="Спросить у них" items={p.result.ask_them} />
                <PrepList title="Риски и как их подать" items={p.result.risks} />
                <PrepList title="Освежить до встречи" items={p.result.prepare} />
              </div>
            )}
            {p.error && <div className="text-caption text-bad">{p.error}</div>}
          </li>
        ))}
      </ul>
    </div>
  )
}

function PrepList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null
  return (
    <div>
      <h4 className="mb-1.5 font-medium">{title}</h4>
      <ul className="list-disc space-y-1 pl-5 text-ink-2">
        {items.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </ul>
    </div>
  )
}
