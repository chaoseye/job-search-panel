import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, ExternalLink, FilePenLine, PanelRightOpen } from 'lucide-react'
import { useState } from 'react'
import { apiGet, apiPut } from '../../api/client'
import type { BoardColumnKey, BoardItem, VacancyDetail } from '../../api/types'
import { ago, manualJournal, outcomeBadge, primaryAction, sendBlock, stageText } from '../../lib/board'
import { formatDateTime } from '../../lib/format'
import { formatsShort, salaryNet } from '../../lib/vacancyView'
import { Badge } from '../ui/Badge'
import { Button, buttonClass } from '../ui/Button'
import { Textarea } from '../ui/Field'
import { ScoreChip } from '../ui/Score'
import { ErrorNotice, Skeleton } from '../ui/States'
import { ActionBar, BoardRow, Facts, Kbd, LetterPreview, Reasons } from './BoardRow'

/** What a row can do; the board owns the requests (and the optimistic removal from the list). */
export interface RowActions {
  primary: (item: BoardItem) => void
  skip: (item: BoardItem) => void
  snooze: (item: BoardItem) => void
  openCard: (item: BoardItem) => void
  reopen: (item: BoardItem) => void
  notSent: (item: BoardItem) => void
  cancelQueued: (item: BoardItem) => void
  outcome: (item: BoardItem, state: 'invitation' | 'discard') => void
}

/** One vacancy of the open column: a line to scan, and under the focused one what it is worth and what to do next. */
export default function VacancyRow({
  colKey,
  item,
  focused,
  mode,
  narrow,
  pending,
  actions,
  onFocus,
}: {
  colKey: BoardColumnKey
  item: BoardItem
  focused: boolean
  mode: 'api' | 'assist'
  narrow: boolean
  pending: boolean
  actions: RowActions
  onFocus: () => void
}) {
  const v = item.vacancy
  const net = salaryNet(v.salary)
  const outcome = outcomeBadge(item.outcome)
  const primary = primaryAction(colKey, item, mode)
  const badges = [
    v.letter_status === 'needs_clarification' && (colKey === 'new' || colKey === 'confirm') && (
      <Badge key="ask" tone="warn">
        Уточнить письмо
      </Badge>
    ),
    outcome && (
      <Badge key="outcome" tone={outcome.tone}>
        {outcome.label}
      </Badge>
    ),
    item.application?.dry_run && (
      <Badge key="dry" tone="outline">
        сухой прогон
      </Badge>
    ),
  ].filter(Boolean)

  return (
    <BoardRow
      id={v.id}
      focused={focused}
      narrow={narrow}
      score={<ScoreChip score={v.score} prescore={v.prescore} />}
      title={v.name}
      meta={
        colKey === 'new'
          ? [v.employer.name ?? 'Работодатель не указан', v.area_name, net ? `${net} на руки` : null]
          : [v.employer.name ?? 'Работодатель не указан', stageText(colKey, item)]
      }
      badges={badges}
      aside={colKey === 'new' ? ago(item.stage_at) : null}
      // a phone swipes the step of the column: right — the next step, left — skip (only a new vacancy can be skipped)
      right={primary && primary.action !== 'card' ? { label: primary.label, run: () => actions.primary(item) } : null}
      left={colKey === 'new' ? { label: 'Пропустить', run: () => actions.skip(item) } : null}
      onFocus={onFocus}
    >
      {colKey === 'new' ? (
        <NewDetails item={item} mode={mode} pending={pending} actions={actions} />
      ) : (
        <RowDetails colKey={colKey} item={item} mode={mode} pending={pending} actions={actions} />
      )}
    </BoardRow>
  )
}

function RowDetails({ colKey, item, mode, pending, actions }: { colKey: BoardColumnKey; item: BoardItem; mode: 'api' | 'assist'; pending: boolean; actions: RowActions }) {
  const app = item.application
  const v = item.vacancy
  const primary = primaryAction(colKey, item, mode)
  const manual = manualJournal(app)
  const facts: string[] = []
  if (app?.sent_at) facts.push(`отправлен ${formatDateTime(app.sent_at)}`)
  if (app?.negotiation?.first_viewed_at) facts.push(`просмотрен ${formatDateTime(app.negotiation.first_viewed_at)}`)
  if (app?.negotiation?.messages_count) facts.push(`сообщений: ${app.negotiation.messages_count}`)
  if (colKey === 'confirm' && app?.queued_at) facts.push(`открыт ${formatDateTime(app.queued_at)}`)
  return (
    <div className="space-y-3 px-4 sm:pl-16">
      <Facts items={facts} />
      {colKey === 'confirm' && app?.mode === 'assist' && (
        <p className="text-sm text-ink-2">Письмо скопировалось, когда вы открыли отклик. Если отклик на hh.ru ушёл, отметьте его отправленным.</p>
      )}
      {colKey === 'confirm' && app?.mode === 'api' && <p className="text-sm text-ink-2">Отклик ждёт своей очереди: панель отправит его через API в пределах лимитов.</p>}
      {app?.letter_text && <LetterPreview text={app.letter_text} lines={4} />}
      {!manual && app?.status === 'sent' && <p className="text-caption text-ink-3">Статус обновляет синхронизация с hh.ru.</p>}
      <ActionBar>
        {primary && (
          <Button variant="primary" size="sm" disabled={pending} onClick={() => actions.primary(item)}>
            {primary.label}
            <Kbd>Enter</Kbd>
          </Button>
        )}
        {colKey === 'confirm' && app?.mode === 'assist' && (
          <>
            <Button size="sm" disabled={pending} onClick={() => actions.notSent(item)}>
              Не отправил
            </Button>
            <Button size="sm" variant="ghost" disabled={pending} onClick={() => actions.reopen(item)}>
              <ExternalLink aria-hidden />
              Открыть ещё раз
            </Button>
          </>
        )}
        {colKey === 'confirm' && app?.mode === 'api' && app.status === 'queued' && (
          <Button size="sm" disabled={pending} onClick={() => actions.cancelQueued(item)}>
            Отменить отклик
          </Button>
        )}
        {manual && (colKey === 'sent' || colKey === 'viewed') && (
          <>
            <Button size="sm" disabled={pending} onClick={() => actions.outcome(item, 'invitation')}>
              Приглашение
            </Button>
            <Button size="sm" disabled={pending} onClick={() => actions.outcome(item, 'discard')}>
              Отказ
            </Button>
          </>
        )}
        {primary?.action !== 'card' && (
          <Button size="sm" variant="ghost" onClick={() => actions.openCard(item)}>
            <PanelRightOpen aria-hidden />
            {colKey === 'confirm' ? 'Карточка вакансии' : 'Карточка отклика'}
          </Button>
        )}
        {v.alternate_url && (
          <a className={buttonClass('ghost', 'sm')} href={v.alternate_url} target="_blank" rel="noreferrer">
            hh.ru
            <ExternalLink aria-hidden />
          </a>
        )}
      </ActionBar>
    </div>
  )
}

/** The focused new vacancy: Claude's reasons and gaps, the letter (questions marked, «Дописать» right here) and the
 *  decision. The card's own data comes from the vacancy detail, the same cache the side card uses. */
function NewDetails({ item, mode, pending, actions }: { item: BoardItem; mode: 'api' | 'assist'; pending: boolean; actions: RowActions }) {
  const qc = useQueryClient()
  const id = item.vacancy.id
  const detail = useQuery({ queryKey: ['vacancy', id], queryFn: () => apiGet<VacancyDetail>(`/api/vacancies/${id}`) })
  const [draft, setDraft] = useState<string | null>(null)
  const save = useMutation({
    mutationFn: (text: string) => apiPut<VacancyDetail>(`/api/vacancies/${id}/letter`, { text }),
    onSuccess: (d) => {
      qc.setQueryData(['vacancy', id], d)
      setDraft(null)
      void qc.invalidateQueries({ queryKey: ['board'] })
      void qc.invalidateQueries({ queryKey: ['board-column'] })
      void qc.invalidateQueries({ queryKey: ['vacancies'] })
      void qc.invalidateQueries({ queryKey: ['vacancy-summary'] })
    },
  })
  const d = detail.data
  const primary = primaryAction('new', item, mode)!
  const v = item.vacancy
  const ev = d?.evaluation
  const letter = d?.letter
  const blockReason = d ? sendBlock(d) : ''

  return (
    <div className="px-4 sm:pl-16">
      {/* the line of the row above already has the employer, the city and the salary */}
      <Facts items={[formatsShort(v), salaryNet(v.salary) ? null : 'зарплата не указана', v.responses_count !== null ? `откликов ${v.responses_count}` : null]} />
      {detail.isLoading && (
        <div className="mt-3 space-y-2">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      )}
      {detail.error && <ErrorNotice className="mt-3" error={detail.error} title="Не удалось загрузить вакансию" onRetry={() => void detail.refetch()} />}
      {d && (
        <div className="mt-3 grid gap-x-6 gap-y-4 @2xl:grid-cols-2">
          <div className="min-w-0 space-y-3">
            {ev ? (
              <>
                {ev.summary && <p className="text-sm text-ink-2">{ev.summary}</p>}
                {ev.matched.length > 0 && <Reasons title="Почему подходит" items={ev.matched} limit={3} />}
                {ev.gaps.length > 0 && <Reasons title="Чего не хватает" items={ev.gaps} />}
                {ev.red_flags.length > 0 && <Reasons title="Настораживает" items={ev.red_flags} />}
              </>
            ) : (
              <p className="text-sm text-ink-2">Claude ещё не разбирал эту вакансию: оценка и письмо появятся после разбора.</p>
            )}
          </div>
          <div className="min-w-0 space-y-2">
            <div className="text-xs font-medium text-ink-3">Письмо</div>
            {draft !== null ? (
              <>
                <Textarea aria-label="Текст письма" className="min-h-48 text-prose" value={draft} autoFocus onChange={(e) => setDraft(e.target.value)} />
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" variant="primary" disabled={save.isPending || !draft.trim() || draft === letter?.text} onClick={() => save.mutate(draft)}>
                    {save.isPending ? 'Сохраняю…' : 'Сохранить'}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setDraft(null)}>
                    Отмена
                  </Button>
                </div>
                {save.error && <ErrorNotice error={save.error} title="Письмо не сохранено" />}
              </>
            ) : letter ? (
              <>
                <LetterPreview text={letter.text} lines={7} />
                {letter.needs_clarification.length > 0 && (
                  <ul className="list-disc pl-5 text-caption text-warn">
                    {letter.needs_clarification.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                )}
                <Button size="sm" variant={v.letter_status === 'needs_clarification' ? 'secondary' : 'ghost'} onClick={() => setDraft(letter.text)}>
                  <FilePenLine aria-hidden />
                  {v.letter_status === 'needs_clarification' ? 'Дописать' : 'Править письмо'}
                </Button>
              </>
            ) : (
              <p className="text-sm text-ink-2">Письма нет: Claude пишет его при разборе. Написать самому можно в карточке вакансии.</p>
            )}
          </div>
        </div>
      )}
      <ActionBar className="mt-3">
        <Button variant="primary" size="sm" disabled={pending || !d || Boolean(blockReason)} onClick={() => actions.primary(item)} title={blockReason || undefined}>
          {blockReason ? 'Сначала уточните письмо' : primary.label}
          {!blockReason && <Kbd>Enter</Kbd>}
        </Button>
        <Button size="sm" disabled={pending} onClick={() => actions.skip(item)}>
          Пропустить
          <Kbd>S</Kbd>
        </Button>
        <Button size="sm" variant="ghost" disabled={pending} onClick={() => actions.snooze(item)}>
          <Clock aria-hidden />
          Отложить на 3 дня
          <Kbd>D</Kbd>
        </Button>
        <Button size="sm" variant="ghost" onClick={() => actions.openCard(item)}>
          <PanelRightOpen aria-hidden />
          Карточка
        </Button>
        {blockReason && <p className="basis-full text-caption text-ink-3">Отклик пока нельзя открыть: {blockReason}.</p>}
      </ActionBar>
    </div>
  )
}
