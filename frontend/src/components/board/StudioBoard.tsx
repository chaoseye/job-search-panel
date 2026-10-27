import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Copy, ExternalLink, EyeOff, PanelRightOpen } from 'lucide-react'
import { useState } from 'react'
import { apiGet, apiPost } from '../../api/client'
import type { SourceColumnList, StudioBoardBand, StudioBoardColumnKey, StudioBoardItem, StudioBoardView, StudioDetail, StudioLetterStyle, StudioLetterView } from '../../api/types'
import { ago } from '../../lib/board'
import {
  isStudioBand,
  STUDIO_BANDS,
  STUDIO_COLUMNS,
  studioBandLabel,
  studioDefaultBand,
  studioEmpty,
  studioKeyHint,
  studioMeta,
  studioPrimary,
  studioSendTarget,
  studioStageText,
  telegramHref,
} from '../../lib/boardSources'
import { formatDateTime } from '../../lib/format'
import { mailtoHref, StudioDrawer } from '../../pages/StudiosPage'
import { Badge } from '../ui/Badge'
import { Button, buttonClass } from '../ui/Button'
import { cx } from '../ui/cx'
import { PageHeader } from '../ui/PageHeader'
import { ScoreChip } from '../ui/Score'
import { Segmented } from '../ui/Segmented'
import { ErrorNotice, Skeleton } from '../ui/States'
import { useMediaQuery } from '../ui/useMediaQuery'
import { SourceSwitch, Toast } from './BoardChrome'
import BoardGrid, { CardShell, ColumnSummary, type GridColumn, type SummaryTile } from './BoardGrid'
import { ActionBar, BoardRow, Facts, Kbd, LetterPreview, Reasons } from './BoardRow'
import FunnelStrip from './FunnelStrip'
import { useBoardKeys, useBoardNav } from './useBoardNav'
import WorkView from './WorkView'

type Kind = 'letter' | 'contacts' | 'sent' | 'replied' | 'declined' | 'skip' | 'hide'
type LetterResult = { queued: number; skipped: string[]; batch_id: string | null }

// where a card goes after the action (the narrow column it lands in glows for a moment); null — it stays or leaves
const DESTINATION: Record<Kind, StudioBoardColumnKey | null> = {
  letter: 'ready',
  contacts: null,
  sent: 'sent',
  replied: 'answer',
  declined: 'answer',
  skip: null,
  hide: null,
}
const DONE: Record<Kind, string> = {
  letter: 'Письмо в очереди для Claude',
  contacts: 'Панель читает сайт студии: контакты появятся через минуту',
  sent: 'Отмечено: письмо отправлено',
  replied: 'Записано: студия ответила',
  declined: 'Записан отказ',
  skip: 'Пропущено',
  hide: 'Скрыто',
}
const STYLE_LABELS: Record<StudioLetterStyle, string> = { business: 'Деловой', simple: 'Простой' }

const itemKey = (item: StudioBoardItem) => item.studio.id

/** The draft on screen: the style the user switched to, else the current (picked) one. */
function chosenLetter(d: StudioDetail, style: StudioLetterStyle | undefined): StudioLetterView | null {
  const variants = d.variants?.length ? d.variants : d.letter ? [d.letter] : []
  return variants.find((v) => v.style === style) ?? d.letter ?? variants[0] ?? null
}

interface Handlers {
  primary: (item: StudioBoardItem) => void
  act: (kind: Kind, item: StudioBoardItem) => void
  markSent: (item: StudioBoardItem) => void
  copy: (item: StudioBoardItem) => void
  openCard: (item: StudioBoardItem) => void
  setStyle: (item: StudioBoardItem, style: StudioLetterStyle) => void
}

function StudioCard({ col, item, onCard }: { col: string; item: StudioBoardItem; onCard: (item: StudioBoardItem) => void }) {
  const s = item.studio
  const key = col as StudioBoardColumnKey
  const badge =
    key === 'ready' && s.status === 'drafting' ? (
      <Badge tone="info">Claude пишет письмо</Badge>
    ) : key === 'answer' ? (
      <Badge tone={s.status === 'declined' ? 'bad' : 'good'}>{s.status === 'declined' ? 'Отказ' : 'Ответили'}</Badge>
    ) : undefined
  return (
    <CardShell
      title={s.name}
      subtitle={[s.domain, s.kind !== 'unknown' ? s.kind_label : null].filter(Boolean).join(' · ') || 'сайт не указан'}
      score={<ScoreChip score={s.fit_score} prescore={null} />}
      badge={badge}
      stage={studioStageText(key, item)}
      onClick={() => onCard(item)}
    />
  )
}

/** The focused studio: what it does and why it fits, its contacts, the letter in two drafts (or the state of the
 *  request), and the step: ask Claude, open the letter in the mail program, mark it sent, record the answer. The data
 *  come from the studio's detail, the same cache its side card uses. */
function StudioDetails({
  col,
  item,
  style,
  opened,
  pending,
  on,
}: {
  col: StudioBoardColumnKey
  item: StudioBoardItem
  style: StudioLetterStyle | undefined
  opened: boolean
  pending: boolean
  on: Handlers
}) {
  const s = item.studio
  const detail = useQuery({ queryKey: ['studio', s.id], queryFn: () => apiGet<StudioDetail>(`/api/studios/${s.id}`) })
  const d = detail.data
  const primary = studioPrimary(col, s, opened)
  const letter = d ? chosenLetter(d, style) : null
  const variants = d?.variants?.length ? d.variants : []
  const waiting = d?.request && ['queued', 'sent_in_batch'].includes(d.request.status)
  const contacts = [...s.emails, s.telegram].filter(Boolean).join(', ')
  return (
    <div className="px-4 sm:pl-16">
      <Facts
        items={[
          s.kind !== 'unknown' ? s.kind_label : null,
          s.area,
          s.services.slice(0, 3).join(', '),
          s.open_vacancies ? `вакансий на hh: ${s.open_vacancies}` : null,
        ]}
      />
      {detail.isLoading && (
        <div className="mt-3 space-y-2">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      )}
      {Boolean(detail.error) && <ErrorNotice className="mt-3" error={detail.error} title="Не удалось загрузить студию" onRetry={() => void detail.refetch()} />}
      {d && (
        <div className="mt-3 grid gap-x-6 gap-y-4 @2xl:grid-cols-2">
          <div className="min-w-0 space-y-3">
            {d.fit_notes.length > 0 && <Reasons title={`Совпадение ${d.fit_score ?? '—'}`} items={d.fit_notes} />}
            {(d.site_summary || d.description) && <p className="line-clamp-4 text-sm text-ink-2">{d.site_summary || d.description}</p>}
            <div className="space-y-0.5 text-sm">
              <div className="text-xs font-medium text-ink-3">Контакты</div>
              {contacts ? <div className="break-words text-ink-2">{contacts}</div> : <div className="text-ink-3">{s.contacts_checked_at ? 'на сайте не нашлось' : 'сайт ещё не проверен'}</div>}
              {d.contacts_error && <div className="text-caption text-warn">{d.contacts_error}</div>}
            </div>
            {s.sent_at && (
              <p className="text-caption text-ink-2">
                Отправлено {formatDateTime(s.sent_at)} ({s.sent_via}: {s.sent_to ?? '—'})
                {s.replied_at ? `, ответ ${formatDateTime(s.replied_at)}${s.reply_subject ? `: «${s.reply_subject}»` : ''}` : ''}
              </p>
            )}
          </div>
          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-medium text-ink-3">Письмо</span>
              {variants.length > 1 && letter && col === 'ready' && (
                <Segmented
                  label="Вариант письма"
                  options={variants.map((v) => [v.style, STYLE_LABELS[v.style] ?? v.style_label] as [StudioLetterStyle, string])}
                  value={letter.style}
                  onChange={(v) => on.setStyle(item, v)}
                />
              )}
            </div>
            {letter ? (
              <>
                <div className="text-sm font-medium text-ink">{letter.subject}</div>
                <LetterPreview text={letter.full_text} lines={8} />
                {letter.needs_clarification.length > 0 && (
                  <ul className="list-disc pl-5 text-caption text-warn">
                    {letter.needs_clarification.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                )}
              </>
            ) : waiting ? (
              <p className="text-sm text-ink-2">{d.request?.batch_id ? `Claude напишет письмо: пакет ${d.request.batch_id} ждёт разбора.` : 'Claude напишет письмо: задача ждёт следующего пакета.'}</p>
            ) : (
              <p className="text-sm text-ink-2">Письма ещё нет: Claude напишет деловой и простой варианты.</p>
            )}
            {d.request?.status === 'cancelled' && d.request.error && <p className="text-caption text-warn">Claude: {d.request.error}</p>}
          </div>
        </div>
      )}
      {opened && <p className="mt-3 text-sm text-ink-2">Письмо открыто для отправки. Отправили? Отметьте.</p>}
      <ActionBar className="mt-3">
        {primary ? (
          <Button variant="primary" size="sm" disabled={pending || ((primary.action === 'mail' || primary.action === 'telegram') && !letter)} onClick={() => on.primary(item)}>
            {primary.label}
            <Kbd>Enter</Kbd>
          </Button>
        ) : (
          <Button variant="primary" size="sm" disabled>
            Claude пишет письмо
          </Button>
        )}
        {col === 'ready' && s.status === 'drafted' && (
          <>
            <Button size="sm" disabled={!letter} onClick={() => on.copy(item)}>
              <Copy aria-hidden />
              Скопировать письмо
            </Button>
            {primary?.action !== 'sent' && (
              <Button size="sm" variant="ghost" disabled={pending || !letter} onClick={() => on.markSent(item)}>
                Уже отправил
              </Button>
            )}
          </>
        )}
        {col === 'sent' && (
          <Button size="sm" disabled={pending} onClick={() => on.act('declined', item)}>
            Отказ
          </Button>
        )}
        {(col === 'new' || col === 'ready') && (
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
        {s.site_url && (
          <a className={buttonClass('ghost', 'sm')} href={s.site_url} target="_blank" rel="noreferrer">
            {s.domain ?? 'Сайт'}
            <ExternalLink aria-hidden />
          </a>
        )}
      </ActionBar>
    </div>
  )
}

/** «Воронка» of the studios: found → the letter ready → sent → answered.
 *  Claude writes the letter in two drafts, the user sends it from their own mailbox or messenger and marks it; the
 *  panel never sends it itself. */
export default function StudioBoard() {
  const qc = useQueryClient()
  const narrow = useMediaQuery('(max-width: 767px)')
  const board = useQuery({ queryKey: ['board', 'studios'], queryFn: () => apiGet<StudioBoardView>('/api/board?source=studios'), refetchInterval: 30000 })
  const bands = board.data?.bands
  const nav = useBoardNav<StudioBoardItem, SourceColumnList<StudioBoardColumnKey, StudioBoardBand, StudioBoardItem>>({
    source: 'studios',
    columnKeys: STUDIO_COLUMNS,
    bands: STUDIO_BANDS,
    defaultBand: studioDefaultBand(bands),
    itemKey,
  })
  const col = nav.col as StudioBoardColumnKey | null
  const band = isStudioBand(nav.band) ? nav.band : null
  const { items, focus } = nav
  const [card, setCard] = useState<number | null>(null)
  // the letters opened in the mail program or Telegram: their next step asks «Да, отправлено»
  const [opened, setOpened] = useState<number[]>([])
  const [styleBy, setStyleBy] = useState<Record<number, StudioLetterStyle>>({})
  const [failed, setFailed] = useState<unknown>(null)

  const refresh = () => {
    for (const key of ['board', 'board-column', 'studios', 'studio', 'studios-summary', 'jobs']) void qc.invalidateQueries({ queryKey: [key] })
  }
  const mutation = useMutation({
    mutationFn: async ({ kind, item, letterId }: { kind: Kind; item: StudioBoardItem; letterId?: number | null }): Promise<unknown> => {
      const s = item.studio
      if (kind === 'letter') {
        const r = await apiPost<LetterResult>('/api/studios/letters', { ids: [s.id] })
        if (!r.queued) throw new Error(r.skipped.join('; ') || 'Письмо не поставлено в очередь')
        return r
      }
      if (kind === 'contacts') return apiPost('/api/studios/contacts', { ids: [s.id] })
      if (kind === 'sent') {
        const target = studioSendTarget(s)
        return apiPost<StudioDetail>(`/api/studios/${s.id}/sent`, { to: target?.to ?? '', via: target?.via ?? 'other', letter_id: letterId ?? null })
      }
      return apiPost(`/api/studios/${s.id}/${kind}`, {})
    },
    // checking the contacts leaves the card where it is until the site is read
    onMutate: ({ kind, item }) => (kind === 'contacts' ? undefined : nav.removeFromList(item.studio.id)),
    onSuccess: (data, { kind, item }) => {
      setOpened((o) => o.filter((id) => id !== item.studio.id))
      nav.setArrived(DESTINATION[kind])
      if (kind === 'sent') {
        const d = data as StudioDetail
        nav.say(
          `${DONE.sent}. Сегодня ${d.sent_today ?? '?'} из ${d.daily_limit ?? '?'}` +
            (d.over_limit ? ': больше дневного лимита, почтовые службы могут счесть ящик спамерским, остальное лучше завтра' : ''),
        )
      } else if (kind === 'letter') {
        const batch = (data as LetterResult).batch_id
        nav.say(batch ? `${DONE.letter}, пакет ${batch}` : DONE.letter)
      } else nav.say(DONE[kind])
    },
    onSettled: refresh,
  })

  const withDetail = (item: StudioBoardItem, go: (d: StudioDetail) => void) => {
    const id = item.studio.id
    const cached = qc.getQueryData<StudioDetail>(['studio', id])
    if (cached) return go(cached)
    qc.fetchQuery({ queryKey: ['studio', id], queryFn: () => apiGet<StudioDetail>(`/api/studios/${id}`) }).then(go, setFailed)
  }
  const copyText = (text: string | undefined): Promise<boolean> =>
    text && navigator.clipboard ? navigator.clipboard.writeText(text).then(() => true, () => false) : Promise.resolve(false)
  const markSent = (item: StudioBoardItem) => withDetail(item, (d) => mutation.mutate({ kind: 'sent', item, letterId: chosenLetter(d, styleBy[item.studio.id])?.id ?? null }))
  const on: Handlers = {
    primary: (item) => {
      const s = item.studio
      const step = col ? studioPrimary(col, s, opened.includes(s.id)) : null
      if (!step) return
      if (step.action === 'card') return setCard(s.id)
      if (step.action === 'sent') return markSent(item)
      if (step.action === 'mail' || step.action === 'telegram')
        return withDetail(item, (d) => {
          const letter = chosenLetter(d, styleBy[s.id])
          if (!letter) return nav.say('Письма ещё нет')
          const target = studioSendTarget(s)
          if (step.action === 'mail' && target?.via === 'email') window.location.assign(mailtoHref(target.to, letter.subject, letter.full_text))
          if (step.action === 'telegram' && s.telegram) {
            // the copying starts while the panel still has the focus, then the chat opens in a new tab
            void copyText(letter.full_text)
            window.open(telegramHref(s.telegram), '_blank', 'noopener')
          }
          setOpened((o) => [...o, s.id])
          nav.say(step.action === 'mail' ? 'Письмо открыто в почте' : 'Telegram открыт, письмо скопировано')
        })
      mutation.mutate({ kind: step.action, item })
    },
    act: (kind, item) => mutation.mutate({ kind, item }),
    markSent,
    copy: (item) =>
      withDetail(item, (d) => {
        const letter = chosenLetter(d, styleBy[item.studio.id])
        void copyText(letter ? `${letter.subject}\n\n${letter.full_text}` : undefined).then((ok) => nav.say(ok ? 'Тема и письмо скопированы' : 'Скопируйте письмо из карточки'))
      }),
    openCard: (item) => setCard(item.studio.id),
    setStyle: (item, style) => setStyleBy((m) => ({ ...m, [item.studio.id]: style })),
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
  const columns: GridColumn<StudioBoardItem>[] = b?.columns ?? []
  const readyCount = b?.columns.find((c) => c.key === 'ready')?.count ?? 0
  const [emptyTitle, emptyBody] = col ? studioEmpty(col, band) : ['', '']
  const tiles = (counts: Record<StudioBoardBand, number>): SummaryTile[] => {
    const all: (SummaryTile | null)[] = [
      { band: 'contacts', label: studioBandLabel('contacts'), count: counts.contacts, tone: 'good' },
      counts.unchecked > 0 ? { band: 'unchecked', label: studioBandLabel('unchecked'), count: counts.unchecked } : null,
      { band: 'nocontacts', label: studioBandLabel('nocontacts'), count: counts.nocontacts, dashed: true },
    ]
    return all.filter((x): x is SummaryTile => x !== null)
  }

  return (
    <div className="flex flex-col gap-4">
      <PageHeader title="Воронка" meta={b ? studioMeta(b.bands, readyCount, b.sent_today, b.daily_limit) : undefined} actions={<SourceSwitch value="studios" />} />
      {board.error && <ErrorNotice error={board.error} title="Не удалось загрузить доску студий" onRetry={() => void board.refetch()} />}
      {board.isLoading && <Skeleton className="h-16 w-full" />}
      {b && !(narrow && col) && <FunnelStrip steps={b.funnel} label="Воронка студий" />}
      {mutation.error && <ErrorNotice error={mutation.error} title="Действие не выполнено" />}
      {Boolean(failed) && <ErrorNotice error={failed} title="Не удалось загрузить студию" onRetry={() => setFailed(null)} />}

      {b && !col && (
        <BoardGrid
          columns={columns}
          narrow={narrow}
          onOpen={nav.openColumn}
          itemKey={itemKey}
          renderCard={(item, key) => <StudioCard col={key} item={item} onCard={on.openCard} />}
          first={
            <ColumnSummary
              tiles={tiles(b.bands)}
              startLabel="Разобрать найденные"
              startBand={studioDefaultBand(b.bands)}
              startDisabled={b.bands.contacts + b.bands.unchecked === 0}
              bestTitle="Лучшие по совпадению"
              best={(columns[0]?.items ?? []).map((item) => (
                <StudioCard key={item.studio.id} col="new" item={item} onCard={on.openCard} />
              ))}
              foot={
                <div className={cx('px-1 text-xs', b.sent_today >= b.daily_limit ? 'text-warn' : 'text-ink-3')}>
                  Отправлено сегодня: {b.sent_today} из {b.daily_limit}
                </div>
              }
              onOpen={nav.openColumn}
            />
          }
        />
      )}
      {b && col && (
        <WorkView
          columns={columns}
          col={col}
          chips={
            col === 'new'
              ? STUDIO_BANDS.filter((x) => x !== 'unchecked' || b.bands.unchecked > 0 || band === 'unchecked').map((x) => ({ band: x, label: studioBandLabel(x), count: b.bands[x] }))
              : []
          }
          chipsLabel="Какие студии показать"
          band={band}
          list={nav.list}
          itemKey={itemKey}
          itemTitle={(item) => item.studio.name}
          renderRow={(item, slot) => {
            const s = item.studio
            const step = studioPrimary(col, s, opened.includes(s.id))
            return (
              <BoardRow
                key={s.id}
                id={s.id}
                focused={slot.focused}
                narrow={narrow}
                score={<ScoreChip score={s.fit_score} prescore={null} />}
                title={s.name}
                meta={[s.domain, studioStageText(col, item)]}
                badges={[
                  col === 'ready' && s.status === 'drafting' && (
                    <Badge key="drafting" tone="info">
                      Claude пишет
                    </Badge>
                  ),
                  col === 'answer' && (
                    <Badge key="answer" tone={s.status === 'declined' ? 'bad' : 'good'}>
                      {s.status === 'declined' ? 'Отказ' : 'Ответили'}
                    </Badge>
                  ),
                ].filter(Boolean)}
                aside={col === 'new' ? ago(item.stage_at) : null}
                right={step && step.action !== 'card' ? { label: step.label, run: () => on.primary(item) } : null}
                left={col === 'new' || col === 'ready' ? { label: 'Пропустить', run: () => on.act('skip', item) } : null}
                onFocus={slot.onFocus}
              >
                <StudioDetails col={col} item={item} style={styleBy[s.id]} opened={opened.includes(s.id)} pending={mutation.isPending} on={on} />
              </BoardRow>
            )
          }}
          miniExtra={(c) =>
            c.key === 'new' ? (
              <>
                <span>
                  <b className="font-semibold tabular-nums text-good">{b.bands.contacts}</b> с контактами
                </span>
                {b.bands.unchecked > 0 && (
                  <span>
                    <b className="font-semibold tabular-nums text-ink">{b.bands.unchecked}</b> не проверены
                  </span>
                )}
                <span>
                  <b className="font-semibold tabular-nums text-ink-2">{b.bands.nocontacts}</b> без контактов
                </span>
              </>
            ) : null
          }
          empty={{
            title: emptyTitle,
            body: emptyBody,
            action:
              col === 'new' && band === 'contacts' && b.bands.unchecked > 0 ? (
                <Button size="sm" onClick={() => nav.setBand('unchecked')}>
                  Показать непроверенные: {b.bands.unchecked}
                </Button>
              ) : undefined,
          }}
          keyHint={studioKeyHint(col)}
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
      {card !== null && <StudioDrawer id={card} onClose={() => setCard(null)} onChanged={refresh} />}
    </div>
  )
}
