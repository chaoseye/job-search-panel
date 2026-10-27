import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, ExternalLink, EyeOff, PanelRightOpen } from 'lucide-react'
import { useState } from 'react'
import { apiGet, apiPost } from '../../api/client'
import type { SourceColumnList, TenderBoardBand, TenderBoardColumnKey, TenderBoardItem, TenderBoardView, TenderDetail } from '../../api/types'
import { ago } from '../../lib/board'
import { formatDateTime } from '../../lib/format'
import {
  isTenderBand,
  TENDER_BANDS,
  TENDER_COLUMNS,
  tenderBandLabel,
  tenderDefaultBand,
  tenderEmpty,
  tenderKeyHint,
  tenderMeta,
  tenderPrimary,
  tenderStageText,
} from '../../lib/boardSources'
import { deadlineText, TenderDrawer } from '../../pages/TendersPage'
import { Badge } from '../ui/Badge'
import { Button, buttonClass } from '../ui/Button'
import { PageHeader } from '../ui/PageHeader'
import { ScoreChip } from '../ui/Score'
import { ErrorNotice, Skeleton } from '../ui/States'
import { useMediaQuery } from '../ui/useMediaQuery'
import { SourceSwitch, Toast } from './BoardChrome'
import BoardGrid, { CardShell, ColumnSummary, type GridColumn } from './BoardGrid'
import { ActionBar, BoardRow, Facts, Kbd, LetterPreview, Reasons } from './BoardRow'
import FunnelStrip from './FunnelStrip'
import { useBoardKeys, useBoardNav } from './useBoardNav'
import WorkView from './WorkView'

type Kind = 'draft' | 'responded' | 'replied' | 'declined' | 'skip' | 'hide'
type DraftResult = { queued: number; skipped: string[]; batch_id: string | null }

// where a card goes after the action (the narrow column it lands in glows for a moment); null — it leaves the board
const DESTINATION: Record<Kind, TenderBoardColumnKey | null> = {
  draft: 'ready',
  responded: 'responded',
  replied: 'answer',
  declined: 'answer',
  skip: null,
  hide: null,
}
const DONE: Record<Kind, string> = {
  draft: 'Отклик в очереди для Claude',
  responded: 'Отмечено: откликнулся',
  replied: 'Записано: заказчик ответил',
  declined: 'Записан отказ заказчика',
  skip: 'Пропущено',
  hide: 'Скрыто',
}

const itemKey = (item: TenderBoardItem) => item.tender.id

interface Handlers {
  primary: (item: TenderBoardItem) => void
  act: (kind: Kind, item: TenderBoardItem) => void
  copy: (item: TenderBoardItem) => void
  openCard: (item: TenderBoardItem) => void
}

function TenderCard({ col, item, onCard }: { col: string; item: TenderBoardItem; onCard: (item: TenderBoardItem) => void }) {
  const t = item.tender
  const key = col as TenderBoardColumnKey
  const badge =
    key === 'ready' && t.status === 'drafting' ? (
      <Badge tone="info">Claude готовит отклик</Badge>
    ) : key === 'ready' && !t.open ? (
      <Badge tone="outline">приём заявок закрыт</Badge>
    ) : key === 'answer' ? (
      <Badge tone={t.status === 'declined' ? 'bad' : 'good'}>{t.status === 'declined' ? 'Отказ' : 'Ответили'}</Badge>
    ) : undefined
  return (
    <CardShell
      title={t.title}
      subtitle={[t.budget ?? 'бюджет не указан', t.section ?? t.service].filter(Boolean).join(' · ')}
      score={<ScoreChip score={t.fit_score} prescore={null} />}
      badge={badge}
      stage={tenderStageText(key, item)}
      onClick={() => onCard(item)}
    />
  )
}

/** The focused tender: its fit, the description and the answer Claude wrote (or the state of the request), and the
 *  step: ask Claude for the answer, post it on workspace.ru, mark it posted. The data come from the tender's detail,
 *  the same cache its side card uses. */
function TenderDetails({ col, item, opened, pending, on }: { col: TenderBoardColumnKey; item: TenderBoardItem; opened: boolean; pending: boolean; on: Handlers }) {
  const t = item.tender
  const detail = useQuery({ queryKey: ['tender', t.id], queryFn: () => apiGet<TenderDetail>(`/api/tenders/${t.id}`) })
  const d = detail.data
  const primary = tenderPrimary(col, t, opened)
  const waiting = d?.request && ['queued', 'sent_in_batch'].includes(d.request.status)
  return (
    <div className="px-4 sm:pl-16">
      {/* the line of the row above already has the budget */}
      <Facts
        items={[
          t.open ? `заявки до ${deadlineText(t.deadline_on, true)}` : 'приём заявок закрыт',
          t.section ?? t.service,
          t.city ? `заказчик из ${t.city}` : null,
          t.participants != null ? `откликов: ${t.participants}` : null,
          t.responded_at ? `откликнулся ${formatDateTime(t.responded_at)}` : null,
          t.answered_at && col === 'answer' ? `${t.status === 'declined' ? 'отказ' : 'ответ'} ${formatDateTime(t.answered_at)}` : null,
        ]}
      />
      {detail.isLoading && (
        <div className="mt-3 space-y-2">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      )}
      {Boolean(detail.error) && <ErrorNotice className="mt-3" error={detail.error} title="Не удалось загрузить тендер" onRetry={() => void detail.refetch()} />}
      {d && (
        <div className="mt-3 grid gap-x-6 gap-y-4 @2xl:grid-cols-2">
          <div className="min-w-0 space-y-3">
            {d.fit_notes.length > 0 && <Reasons title={`Совпадение ${d.fit_score ?? '—'}`} items={d.fit_notes} />}
            <div className="space-y-1">
              <div className="text-xs font-medium text-ink-3">Описание</div>
              {d.description ? <LetterPreview text={d.description} lines={6} /> : <p className="text-sm text-ink-2">{d.detail_error ?? 'Карточку тендера ещё не прочитали: откроется при следующей проверке.'}</p>}
            </div>
          </div>
          <div className="min-w-0 space-y-2">
            <div className="text-xs font-medium text-ink-3">Отклик</div>
            {d.reply ? (
              <>
                <LetterPreview text={d.reply.text} lines={8} />
                {d.reply.needs_clarification.length > 0 && (
                  <ul className="list-disc pl-5 text-caption text-warn">
                    {d.reply.needs_clarification.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                )}
                {d.reply.warnings.length > 0 && <p className="text-caption text-warn">Замечания: {d.reply.warnings.join('; ')}</p>}
              </>
            ) : waiting ? (
              <p className="text-sm text-ink-2">{d.request?.batch_id ? `Claude напишет отклик: пакет ${d.request.batch_id} ждёт разбора.` : 'Claude напишет отклик: задача ждёт следующего пакета.'}</p>
            ) : (
              <p className="text-sm text-ink-2">Отклика ещё нет: Claude напишет его по тексту тендера.</p>
            )}
            {d.request?.status === 'cancelled' && d.request.error && <p className="text-caption text-warn">Claude: {d.request.error}</p>}
          </div>
        </div>
      )}
      {opened && <p className="mt-3 text-sm text-ink-2">Отклик скопирован, тендер открыт на workspace.ru. Опубликовали? Отметьте.</p>}
      <ActionBar className="mt-3">
        {primary ? (
          <Button variant="primary" size="sm" disabled={pending || (primary.action === 'open' && !d)} onClick={() => on.primary(item)}>
            {primary.label}
            <Kbd>Enter</Kbd>
          </Button>
        ) : (
          <Button variant="primary" size="sm" disabled>
            Claude готовит отклик
          </Button>
        )}
        {col === 'ready' && t.status === 'drafted' && (
          <>
            <Button size="sm" disabled={!d?.reply} onClick={() => on.copy(item)}>
              <Copy aria-hidden />
              Скопировать отклик
            </Button>
            {primary?.action !== 'responded' && (
              <Button size="sm" variant="ghost" disabled={pending} onClick={() => on.act('responded', item)}>
                Уже откликнулся
              </Button>
            )}
          </>
        )}
        {col === 'responded' && (
          <Button size="sm" disabled={pending} onClick={() => on.act('declined', item)}>
            Отказ
          </Button>
        )}
        {(col === 'new' || col === 'ready') && primary?.action !== 'skip' && (
          <Button size="sm" disabled={pending} onClick={() => on.act('skip', item)}>
            Пропустить
            <Kbd>S</Kbd>
          </Button>
        )}
        {col === 'new' && (
          <Button size="sm" variant="ghost" disabled={pending} onClick={() => on.act('hide', item)}>
            <EyeOff aria-hidden />
            Скрыть
          </Button>
        )}
        {primary?.action !== 'card' && (
          <Button size="sm" variant="ghost" onClick={() => on.openCard(item)}>
            <PanelRightOpen aria-hidden />
            Карточка
          </Button>
        )}
        <a className={buttonClass('ghost', 'sm')} href={t.url} target="_blank" rel="noreferrer">
          workspace.ru
          <ExternalLink aria-hidden />
        </a>
      </ActionBar>
    </div>
  )
}

/** «Воронка» of the tenders of workspace.ru: found → the answer ready →
 *  responded → the organizer's answer. Claude writes the answer from the tender's text, the user posts it on
 *  workspace.ru and marks it, then records what the organizer answered (the site tells the user, not the panel). */
export default function TenderBoard() {
  const qc = useQueryClient()
  const narrow = useMediaQuery('(max-width: 767px)')
  const board = useQuery({ queryKey: ['board', 'tenders'], queryFn: () => apiGet<TenderBoardView>('/api/board?source=tenders'), refetchInterval: 30000 })
  const bands = board.data?.bands
  const nav = useBoardNav<TenderBoardItem, SourceColumnList<TenderBoardColumnKey, TenderBoardBand, TenderBoardItem>>({
    source: 'tenders',
    columnKeys: TENDER_COLUMNS,
    bands: TENDER_BANDS,
    defaultBand: tenderDefaultBand(bands),
    itemKey,
  })
  const col = nav.col as TenderBoardColumnKey | null
  const band = isTenderBand(nav.band) ? nav.band : null
  const { items, focus } = nav
  const [card, setCard] = useState<number | null>(null)
  // the answers opened on workspace.ru: their next step asks «Да, откликнулся»
  const [opened, setOpened] = useState<number[]>([])
  const [failed, setFailed] = useState<unknown>(null)

  const refresh = () => {
    for (const key of ['board', 'board-column', 'tenders', 'tender', 'tenders-summary']) void qc.invalidateQueries({ queryKey: [key] })
  }
  const mutation = useMutation({
    mutationFn: async ({ kind, item }: { kind: Kind; item: TenderBoardItem }): Promise<DraftResult | unknown> => {
      const id = item.tender.id
      if (kind === 'draft') {
        const r = await apiPost<DraftResult>('/api/tenders/replies', { ids: [id] })
        if (!r.queued) throw new Error(r.skipped.join('; ') || 'Отклик не поставлен в очередь')
        return r
      }
      return apiPost(`/api/tenders/${id}/${kind}`, {})
    },
    onMutate: ({ item }) => nav.removeFromList(item.tender.id),
    onSuccess: (data, { kind, item }) => {
      setOpened((o) => o.filter((id) => id !== item.tender.id))
      nav.setArrived(DESTINATION[kind])
      const batch = kind === 'draft' ? (data as DraftResult).batch_id : null
      nav.say(batch ? `${DONE.draft}, пакет ${batch}` : DONE[kind])
    },
    onSettled: refresh,
  })

  const withDetail = (item: TenderBoardItem, go: (d: TenderDetail) => void) => {
    const id = item.tender.id
    const cached = qc.getQueryData<TenderDetail>(['tender', id])
    if (cached) return go(cached)
    qc.fetchQuery({ queryKey: ['tender', id], queryFn: () => apiGet<TenderDetail>(`/api/tenders/${id}`) }).then(go, setFailed)
  }
  const copyReply = (d: TenderDetail): Promise<boolean> =>
    d.reply && navigator.clipboard ? navigator.clipboard.writeText(d.reply.text).then(() => true, () => false) : Promise.resolve(false)
  const on: Handlers = {
    primary: (item) => {
      const step = col ? tenderPrimary(col, item.tender, opened.includes(item.tender.id)) : null
      if (!step) return
      if (step.action === 'card') return setCard(item.tender.id)
      if (step.action === 'open')
        return withDetail(item, (d) => {
          // the copying starts while the panel still has the focus, then the tender opens in a new tab
          void copyReply(d)
          window.open(item.tender.url, '_blank', 'noopener')
          setOpened((o) => [...o, item.tender.id])
          nav.say(d.reply ? 'Тендер открыт на workspace.ru, отклик скопирован' : 'Тендер открыт на workspace.ru')
        })
      mutation.mutate({ kind: step.action, item })
    },
    act: (kind, item) => mutation.mutate({ kind, item }),
    copy: (item) => withDetail(item, (d) => void copyReply(d).then((ok) => nav.say(ok ? 'Отклик скопирован' : 'Скопируйте отклик из карточки'))),
    openCard: (item) => setCard(item.tender.id),
  }

  useBoardKeys(col !== null, (e) => {
    if (nav.walk(e)) return true
    const item = items[focus]
    if (!item) return false
    if (e.key === 'Enter') on.primary(item)
    else if (e.code === 'KeyS' && (col === 'new' || col === 'ready')) on.act('skip', item)
    else return false
    return true
  })

  const b = board.data
  const columns: GridColumn<TenderBoardItem>[] = b?.columns ?? []
  const readyCount = b?.columns.find((c) => c.key === 'ready')?.count ?? 0
  const [emptyTitle, emptyBody] = col ? tenderEmpty(col, band) : ['', '']

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Воронка" meta={b ? tenderMeta(b.bands, readyCount) : undefined} actions={<SourceSwitch value="tenders" />} />
      {board.error && <ErrorNotice error={board.error} title="Не удалось загрузить доску тендеров" onRetry={() => void board.refetch()} />}
      {board.isLoading && <Skeleton className="h-16 w-full" />}
      {b && !(narrow && col) && <FunnelStrip steps={b.funnel} label="Воронка тендеров за 30 дней" />}
      {mutation.error && <ErrorNotice error={mutation.error} title="Действие не выполнено" />}
      {Boolean(failed) && <ErrorNotice error={failed} title="Не удалось загрузить тендер" onRetry={() => setFailed(null)} />}

      {b && !col && (
        <BoardGrid
          columns={columns}
          narrow={narrow}
          onOpen={nav.openColumn}
          itemKey={itemKey}
          renderCard={(item, key) => <TenderCard col={key} item={item} onCard={on.openCard} />}
          first={
            <ColumnSummary
              fresh={b.fresh_today}
              tiles={[
                { band: 'relevant', label: tenderBandLabel('relevant'), count: b.bands.relevant, tone: 'good' },
                { band: 'other', label: tenderBandLabel('other'), count: b.bands.other, dashed: true },
              ]}
              startLabel="Разобрать найденные"
              startBand={tenderDefaultBand(b.bands)}
              startDisabled={b.bands.relevant + b.bands.other === 0}
              bestTitle="Лучшие по совпадению"
              best={(columns[0]?.items ?? []).map((item) => (
                <TenderCard key={item.tender.id} col="new" item={item} onCard={on.openCard} />
              ))}
              onOpen={nav.openColumn}
            />
          }
        />
      )}
      {b && col && (
        <WorkView
          columns={columns}
          col={col}
          chips={col === 'new' ? TENDER_BANDS.map((x) => ({ band: x, label: tenderBandLabel(x), count: b.bands[x] })) : []}
          chipsLabel="Какие тендеры показать"
          band={band}
          list={nav.list}
          itemKey={itemKey}
          itemTitle={(item) => item.tender.title}
          renderRow={(item, slot) => (
            <BoardRow
              key={item.tender.id}
              id={item.tender.id}
              focused={slot.focused}
              narrow={narrow}
              score={<ScoreChip score={item.tender.fit_score} prescore={null} />}
              title={item.tender.title}
              meta={[
                item.tender.budget ?? 'бюджет не указан',
                col === 'new' ? `заявки до ${deadlineText(item.tender.deadline_on, item.tender.open)}` : tenderStageText(col, item),
              ]}
              badges={[
                col === 'ready' && item.tender.status === 'drafting' && (
                  <Badge key="drafting" tone="info">
                    Claude готовит
                  </Badge>
                ),
                col === 'ready' && !item.tender.open && (
                  <Badge key="closed" tone="outline">
                    заявки закрыты
                  </Badge>
                ),
                col === 'answer' && (
                  <Badge key="answer" tone={item.tender.status === 'declined' ? 'bad' : 'good'}>
                    {item.tender.status === 'declined' ? 'Отказ' : 'Ответили'}
                  </Badge>
                ),
              ].filter(Boolean)}
              aside={col === 'new' ? ago(item.stage_at) : null}
              right={(() => {
                const step = tenderPrimary(col, item.tender, opened.includes(item.tender.id))
                return step && step.action !== 'card' ? { label: step.label, run: () => on.primary(item) } : null
              })()}
              left={col === 'new' || col === 'ready' ? { label: 'Пропустить', run: () => on.act('skip', item) } : null}
              onFocus={slot.onFocus}
            >
              <TenderDetails col={col} item={item} opened={opened.includes(item.tender.id)} pending={mutation.isPending} on={on} />
            </BoardRow>
          )}
          miniExtra={(c) =>
            c.key === 'new' ? (
              <>
                <span>
                  <b className="font-semibold tabular-nums text-good">{b.bands.relevant}</b> подходят
                </span>
                <span>
                  <b className="font-semibold tabular-nums text-ink-2">{b.bands.other}</b> остальные открытые
                </span>
              </>
            ) : null
          }
          empty={{
            title: emptyTitle,
            body: emptyBody,
            action:
              col === 'new' && band === 'relevant' && b.bands.other > 0 ? (
                <Button size="sm" onClick={() => nav.setBand('other')}>
                  Показать остальные открытые: {b.bands.other}
                </Button>
              ) : undefined,
          }}
          keyHint={tenderKeyHint(col)}
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
      {card !== null && <TenderDrawer id={card} onClose={() => setCard(null)} onChanged={refresh} />}
    </div>
  )
}
