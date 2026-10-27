import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { apiGet, apiPost } from '../../api/client'
import type { ApplicationRow, ApplyStatus, BoardBand, BoardColumnKey, BoardColumnList, BoardItem, BoardView, QueueResult, VacancyDetail } from '../../api/types'
import { BANDS, bandChips, bandLabel, COLUMN_KEYS, defaultBand, emptyText, keyHint, outcomeBadge, primaryAction, sendBlock, stageText } from '../../lib/board'
import { countOf } from '../../lib/format'
import AssistPanel from '../AssistPanel'
import ConsentModal from '../ConsentModal'
import NegotiationDrawer from '../NegotiationDrawer'
import VacancyDrawer from '../VacancyDrawer'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { PageHeader } from '../ui/PageHeader'
import { ScoreChip } from '../ui/Score'
import { ErrorNotice, Notice, Skeleton } from '../ui/States'
import { useMediaQuery } from '../ui/useMediaQuery'
import { SourceSwitch, Toast } from './BoardChrome'
import BoardGrid, { CardShell, ColumnSummary, type GridColumn, type SummaryTile } from './BoardGrid'
import FunnelStrip from './FunnelStrip'
import { useBoardKeys, useBoardNav } from './useBoardNav'
import VacancyRow, { type RowActions } from './VacancyRow'
import WorkView from './WorkView'

const SNOOZE_DAYS = 3

type Kind = 'skip' | 'snooze' | 'open' | 'queue' | 'confirm' | 'notSent' | 'cancel' | 'viewed' | 'invitation' | 'discard'

// where a card goes after the action (the narrow column it lands in glows for a moment); null — it leaves the board
const DESTINATION: Record<Kind, BoardColumnKey | null> = {
  skip: null,
  snooze: null,
  open: 'confirm',
  queue: 'confirm',
  confirm: 'sent',
  notSent: 'new',
  cancel: 'new',
  viewed: 'viewed',
  invitation: 'answer',
  discard: 'answer',
}

const DONE: Record<Kind, string> = {
  skip: 'Пропущено',
  snooze: `Отложено на ${SNOOZE_DAYS} дня`,
  open: 'Страница отклика открыта, письмо скопировано',
  queue: 'Отклик в очереди на отправку',
  confirm: 'Отклик отмечен отправленным',
  notSent: 'Вакансия вернулась в «Новые»',
  cancel: 'Отклик отменён',
  viewed: 'Записано: работодатель смотрел',
  invitation: 'Записано приглашение',
  discard: 'Записан отказ',
}

const itemKey = (item: BoardItem) => item.vacancy.id

function tile(band: string, label: string, count: number, extra: Omit<SummaryTile, 'band' | 'label' | 'count'> = {}): SummaryTile {
  return { band, label, count, ...extra }
}

/** A vacancy on the board: what it is, its score and the step it waits at. */
function VacancyCard({ col, item, onCard }: { col: string; item: BoardItem; onCard: (item: BoardItem, col: string) => void }) {
  const v = item.vacancy
  const key = col as BoardColumnKey
  const outcome = outcomeBadge(item.outcome)
  const asks = v.letter_status === 'needs_clarification' && (key === 'new' || key === 'confirm')
  return (
    <CardShell
      title={v.name}
      subtitle={v.employer.name ?? 'Работодатель не указан'}
      score={<ScoreChip score={v.score} prescore={v.prescore} />}
      badge={asks ? <Badge tone="warn">Уточнить письмо</Badge> : outcome ? <Badge tone={outcome.tone}>{outcome.label}</Badge> : undefined}
      stage={stageText(key, item)}
      onClick={() => onCard(item, col)}
    />
  )
}

/** «Воронка» of vacancies: the board of the vacancy pipeline with the 30-day funnel over
 *  it; a column opens right on the board as a working list (`?col=new&band=hi`), its cards move on to the next column
 *  by Enter, a swipe or a button. Actions are the usual requests of «Вакансии» and «Отклики». */
export default function VacancyBoard() {
  const qc = useQueryClient()
  const narrow = useMediaQuery('(max-width: 767px)')
  const board = useQuery({ queryKey: ['board', 'vacancies'], queryFn: () => apiGet<BoardView>('/api/board'), refetchInterval: 30000 })
  const applyStatus = useQuery({ queryKey: ['apply-status'], queryFn: () => apiGet<ApplyStatus>('/api/applications/status') })
  const mode = applyStatus.data?.apply_mode ?? 'assist'
  const nav = useBoardNav<BoardItem, BoardColumnList>({ source: 'vacancies', columnKeys: COLUMN_KEYS, bands: BANDS, defaultBand: defaultBand(board.data?.new), itemKey })
  const col = nav.col as BoardColumnKey | null
  const band = nav.band as BoardBand | null
  const { items, focus } = nav

  const [vacancyCard, setVacancyCard] = useState<number | null>(null)
  const [applicationCard, setApplicationCard] = useState<ApplicationRow | null>(null)
  const [assist, setAssist] = useState<ApplicationRow | null>(null)
  const [consentThen, setConsentThen] = useState<null | (() => void)>(null)
  const [apiConfirm, setApiConfirm] = useState<BoardItem | null>(null)

  const refresh = () => {
    for (const key of ['board', 'board-column', 'vacancies', 'vacancy-summary', 'applications', 'apply-status', 'negotiation', 'stats', 'sync-status'])
      void qc.invalidateQueries({ queryKey: [key] })
  }

  const act = useMutation({
    mutationFn: async ({ kind, item }: { kind: Kind; item: BoardItem }): Promise<unknown> => {
      const vid = item.vacancy.id
      const aid = item.application?.id
      switch (kind) {
        case 'skip':
          return apiPost(`/api/vacancies/${vid}/skip`, { reason: null })
        case 'snooze':
          return apiPost(`/api/vacancies/${vid}/snooze`, { days: SNOOZE_DAYS })
        case 'open':
          return apiPost<ApplicationRow>('/api/applications/assist/open', { vacancy_id: vid })
        case 'queue': {
          const r = await apiPost<QueueResult>('/api/applications/queue', { vacancy_ids: [vid] })
          if (r.rejected.length) throw new Error(r.rejected.map((x) => x.reason).join('; '))
          return r
        }
        case 'confirm':
        case 'notSent':
          return apiPost(`/api/applications/${aid}/confirm`, { sent: kind === 'confirm' })
        case 'cancel':
          return apiPost(`/api/applications/${aid}/cancel`, {})
        case 'viewed':
          return apiPost(`/api/applications/${aid}/manual-update`, { viewed: true })
        case 'invitation':
        case 'discard':
          return apiPost(`/api/applications/${aid}/manual-update`, { state: kind })
      }
    },
    onMutate: ({ item }) => nav.removeFromList(item.vacancy.id),
    onSuccess: (data, { kind }) => {
      if (kind === 'open') setAssist(data as ApplicationRow)
      nav.setArrived(DESTINATION[kind])
      nav.say(DONE[kind])
    },
    onSettled: refresh,
  })

  const needsConsent = applyStatus.data?.consent_accepted_at === null
  const withConsent = (fn: () => void) => (needsConsent ? setConsentThen(() => fn) : fn())

  // a new vacancy goes to hh.ru only with a letter that can be sent: the button, Enter and a swipe check the same rule
  // (the focused card's detail is in the cache already, a swiped one is fetched)
  const [checkFailed, setCheckFailed] = useState<{ error: unknown; retry: () => void } | null>(null)
  const checkSend = (item: BoardItem, go: () => void) => {
    const id = item.vacancy.id
    qc.fetchQuery({ queryKey: ['vacancy', id], queryFn: () => apiGet<VacancyDetail>(`/api/vacancies/${id}`) }).then(
      (d) => {
        setCheckFailed(null)
        const block = sendBlock(d)
        if (block) nav.say(`Отклик пока нельзя открыть: ${block}`)
        else withConsent(go)
      },
      (error: unknown) => setCheckFailed({ error, retry: () => checkSend(item, go) }),
    )
  }

  const actions: RowActions = {
    primary: (item) => {
      const step = col ? primaryAction(col, item, mode) : null
      if (!step) return
      if (step.action === 'card') return setApplicationCard(item.application)
      if (step.action === 'open') return checkSend(item, () => act.mutate({ kind: 'open', item }))
      if (step.action === 'queue') return checkSend(item, () => setApiConfirm(item))
      act.mutate({ kind: step.action, item })
    },
    skip: (item) => act.mutate({ kind: 'skip', item }),
    snooze: (item) => act.mutate({ kind: 'snooze', item }),
    openCard: (item) => (item.application && (col === 'sent' || col === 'viewed' || col === 'answer') ? setApplicationCard(item.application) : setVacancyCard(item.vacancy.id)),
    reopen: (item) => item.application && setAssist({ ...item.application, apply_url: item.vacancy.apply_alternate_url ?? item.vacancy.alternate_url }),
    notSent: (item) => act.mutate({ kind: 'notSent', item }),
    cancelQueued: (item) => act.mutate({ kind: 'cancel', item }),
    outcome: (item, state) => act.mutate({ kind: state, item }),
  }

  useBoardKeys(col !== null, (e) => {
    if (nav.walk(e)) return true
    const item = items[focus]
    if (!item) return false
    if (e.key === 'Enter') actions.primary(item)
    else if (e.code === 'KeyS' && col === 'new') actions.skip(item)
    else if (e.code === 'KeyD' && col === 'new') actions.snooze(item)
    else return false
    return true
  })

  const b = board.data
  const t = b?.thresholds
  const n = b?.new
  const columns: GridColumn<BoardItem>[] = b?.columns ?? []
  const confirmCount = b?.columns.find((c) => c.key === 'confirm')?.count ?? 0
  const meta = n
    ? [
        countOf(n.hi + n.mid, ['вакансия ждёт решения', 'вакансии ждут решения', 'вакансий ждут решения']),
        confirmCount > 0 ? countOf(confirmCount, ['отклик ждёт подтверждения', 'отклика ждут подтверждения', 'откликов ждут подтверждения']) : null,
      ]
        .filter(Boolean)
        .join(' · ')
    : undefined
  const onCard = (item: BoardItem, key: string) => (item.application && key !== 'confirm' ? setApplicationCard(item.application) : setVacancyCard(item.vacancy.id))
  const [emptyTitle, emptyBody] = col ? emptyText(col, band) : ['', '']

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Воронка" meta={meta} actions={<SourceSwitch value="vacancies" />} />
      {board.error && <ErrorNotice error={board.error} title="Не удалось загрузить доску" onRetry={() => void board.refetch()} />}
      {board.isLoading && <Skeleton className="h-16 w-full" />}
      {b && !(narrow && col) && <FunnelStrip steps={b.funnel} label="Воронка за 30 дней" />}

      {act.error && <ErrorNotice error={act.error} title="Действие не выполнено" />}
      {checkFailed && <ErrorNotice error={checkFailed.error} title="Не удалось проверить письмо перед откликом" onRetry={checkFailed.retry} />}
      {apiConfirm && (
        <Notice
          tone="info"
          title={`Отправить отклик через API hh.ru: «${apiConfirm.vacancy.name}»?`}
          action={
            <div className="flex gap-2">
              <Button
                variant="primary"
                size="sm"
                onClick={() => {
                  act.mutate({ kind: 'queue', item: apiConfirm })
                  setApiConfirm(null)
                }}
              >
                Да, отправить
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setApiConfirm(null)}>
                Отмена
              </Button>
            </div>
          }
        >
          Отклик уйдёт в очередь и отправится в пределах дневного и часового лимитов.{applyStatus.data?.dry_run ? ' Включён сухой прогон: на hh.ru ничего не отправится.' : ''}
        </Notice>
      )}
      {assist && (
        <AssistPanel
          application={assist}
          onDone={(sent) => {
            setAssist(null)
            nav.setArrived(sent ? 'sent' : 'new')
            nav.say(sent ? DONE.confirm : DONE.notSent)
            refresh()
          }}
        />
      )}
      {consentThen && (
        <ConsentModal
          onAccepted={() => {
            const fn = consentThen
            setConsentThen(null)
            void qc.invalidateQueries({ queryKey: ['apply-status'] })
            fn()
          }}
          onClose={() => setConsentThen(null)}
        />
      )}

      {b && t && n && !col && (
        <BoardGrid
          columns={columns}
          narrow={narrow}
          onOpen={nav.openColumn}
          itemKey={itemKey}
          renderCard={(item, key) => <VacancyCard col={key} item={item} onCard={onCard} />}
          first={
            <ColumnSummary
              fresh={n.fresh_today}
              tiles={[
                tile('hi', `Оценка от ${t.suitable}`, n.hi, { tone: 'good' }),
                tile('mid', `Оценка ${t.maybe}–${t.suitable - 1}`, n.mid, { tone: 'warn' }),
                n.clarify > 0 && tile('clarify', 'Письмо ждёт уточнения', n.clarify, { tone: 'warn' }),
                tile('low', `Ниже ${t.maybe}, скрыты`, n.low, { dashed: true }),
                n.wait > 0 && tile('wait', 'Claude ещё разбирает', n.wait),
                n.manual > 0 && tile('manual', 'Нужно действие на hh.ru', n.manual, { tone: 'warn' }),
              ].filter((x): x is SummaryTile => Boolean(x))}
              startLabel="Разобрать новые"
              startBand={defaultBand(n)}
              startDisabled={n.hi + n.mid + n.manual === 0}
              bestTitle="Лучшие по оценке"
              best={(columns[0]?.items ?? []).map((item) => (
                <VacancyCard key={item.vacancy.id} col="new" item={item} onCard={onCard} />
              ))}
              foot={n.snoozed > 0 ? <div className="px-1 text-xs text-ink-3">Отложено: {n.snoozed}</div> : undefined}
              onOpen={nav.openColumn}
            />
          }
        />
      )}
      {b && t && n && col && (
        <WorkView
          columns={columns}
          col={col}
          chips={col === 'new' ? bandChips(n, t, band).map((c) => ({ band: c.band, label: c.label, count: c.count })) : []}
          chipsLabel="Какие вакансии показать"
          band={band}
          list={nav.list}
          itemKey={itemKey}
          itemTitle={(item) => item.vacancy.name}
          renderRow={(item, slot) => (
            <VacancyRow key={item.vacancy.id} colKey={col} item={item} focused={slot.focused} mode={mode} narrow={narrow} pending={act.isPending} actions={actions} onFocus={slot.onFocus} />
          )}
          miniExtra={(c) =>
            c.key === 'new' ? (
              <>
                <span>
                  <b className="font-semibold tabular-nums text-good">{n.hi}</b> от {t.suitable}
                </span>
                <span>
                  <b className="font-semibold tabular-nums text-warn">{n.mid}</b> {t.maybe}–{t.suitable - 1}
                </span>
                <span>
                  <b className="font-semibold tabular-nums text-ink-2">{n.low}</b> ниже {t.maybe}, скрыты
                </span>
              </>
            ) : null
          }
          empty={{
            title: emptyTitle,
            body: emptyBody,
            action:
              col === 'new' && band === 'hi' && n.mid > 0 ? (
                <Button size="sm" onClick={() => nav.setBand('mid')}>
                  Показать {bandLabel('mid', t)}: {n.mid}
                </Button>
              ) : undefined,
          }}
          keyHint={keyHint(col, mode)}
          narrow={narrow}
          arrived={nav.arrived}
          focus={focus}
          growFrom={nav.growFrom}
          onFocus={nav.setFocus}
          onSwitch={(key, from) => nav.openColumn(key, undefined, from)}
          onBand={nav.setBand}
          onClose={nav.closeColumn}
          onMore={nav.more}
        />
      )}

      <Toast said={nav.said} />

      {vacancyCard !== null && (
        <VacancyDrawer
          id={vacancyCard}
          applyMode={mode}
          onClose={() => setVacancyCard(null)}
          onChanged={refresh}
          onApply={(id) => {
            const item = items.find((x) => x.vacancy.id === id) ?? b?.columns.flatMap((c) => c.items).find((x) => x.vacancy.id === id)
            setVacancyCard(null)
            if (!item) return
            withConsent(() => (mode === 'api' ? setApiConfirm(item) : act.mutate({ kind: 'open', item })))
          }}
        />
      )}
      {applicationCard && (
        <NegotiationDrawer
          application={applicationCard}
          onClose={() => {
            setApplicationCard(null)
            refresh()
          }}
        />
      )}
    </div>
  )
}
