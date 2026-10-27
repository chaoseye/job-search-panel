// The board «Воронка»: what its columns and bands are called, where a card stands and since when, what its next step
// is, the conversions of the funnel. Pure functions — the page renders, this decides.
import type { ApplicationRow, BoardBand, BoardColumnKey, BoardFunnelStep, BoardItem, BoardNewCounts, VacancyDetail } from '../api/types'
import type { BadgeTone } from '../components/ui/Badge'
import { shortAgo } from './format'

export const COLUMN_KEYS: BoardColumnKey[] = ['new', 'confirm', 'sent', 'viewed', 'answer']
export const BANDS: BoardBand[] = ['hi', 'mid', 'clarify', 'low', 'wait', 'manual']

export function isColumnKey(value: string | null | undefined): value is BoardColumnKey {
  return COLUMN_KEYS.includes(value as BoardColumnKey)
}

export function isBand(value: string | null | undefined): value is BoardBand {
  return BANDS.includes(value as BoardBand)
}

/** The column to the left (-1) or to the right (1), null at the edge. */
export function neighbourColumn(key: BoardColumnKey, step: 1 | -1): BoardColumnKey | null {
  return COLUMN_KEYS[COLUMN_KEYS.indexOf(key) + step] ?? null
}

export interface Thresholds {
  suitable: number
  maybe: number
}

export function bandLabel(band: BoardBand, t: Thresholds): string {
  switch (band) {
    case 'hi':
      return `От ${t.suitable}`
    case 'mid':
      return `${t.maybe}–${t.suitable - 1}`
    case 'clarify':
      return 'Уточнить письмо'
    case 'low':
      return `Ниже ${t.maybe}`
    case 'wait':
      return 'Ждут оценки Claude'
    case 'manual':
      return 'Нужно действие на hh.ru'
  }
}

export interface BandChip {
  band: BoardBand
  label: string
  count: number
  attention: boolean
}

/** The chips of the open «Новые»: the two decision bands and the rejected ones always; a question in a letter, the
 *  queue of Claude and a step on hh.ru only while there is something (or while the chip is the open one). */
export function bandChips(counts: BoardNewCounts, t: Thresholds, open: BoardBand | null): BandChip[] {
  return BANDS.filter((b) => ['hi', 'mid', 'low'].includes(b) || counts[b as keyof BoardNewCounts] > 0 || b === open).map((band) => ({
    band,
    label: bandLabel(band, t),
    count: counts[band as keyof BoardNewCounts],
    attention: band === 'clarify' || band === 'manual',
  }))
}

/** The band «Новые» opens on from the board: the best one that has cards. */
export function defaultBand(counts: BoardNewCounts | undefined): BoardBand {
  if (!counts || counts.hi > 0) return 'hi'
  if (counts.mid > 0) return 'mid'
  if (counts.manual > 0) return 'manual'
  return 'hi'
}

export interface FunnelCell extends BoardFunnelStep {
  // percent of the step before, rounded; null for the first step or after an empty one
  conv: number | null
  weak: boolean
}

/** Conversion from the step before and the weakest transition past the first one (found → scored is a filter, not a
 *  step of the response): there the search loses the most. */
export function funnelCells(steps: BoardFunnelStep[]): FunnelCell[] {
  let weakest = -1
  let worst = Infinity
  steps.forEach((s, i) => {
    const prev = steps[i - 1]
    if (i > 1 && prev && prev.value > 0 && s.value / prev.value < worst) {
      worst = s.value / prev.value
      weakest = i
    }
  })
  return steps.map((s, i) => {
    const prev = steps[i - 1]
    return { ...s, conv: prev && prev.value > 0 ? Math.round((s.value * 100) / prev.value) : null, weak: i === weakest }
  })
}

/** «3 ч назад», «6 дн. назад», then the date alone: «13.10». */
export function ago(iso: string | null | undefined, now: Date = new Date()): string {
  const s = shortAgo(iso, now)
  if (s === 'сейчас') return 'только что'
  return /(мин|ч|дн\.)$/.test(s) ? `${s} назад` : s
}

/** How long something waits: «6 дн.», «с 13.10». */
function waiting(iso: string | null | undefined, now: Date): string {
  const s = shortAgo(iso, now)
  if (s === 'сейчас') return 'меньше минуты'
  return /(мин|ч|дн\.)$/.test(s) ? s : `с ${s}`
}

function isToday(iso: string | null | undefined, now: Date): boolean {
  if (!iso) return false
  const d = new Date(iso)
  return d.toDateString() === now.toDateString()
}

/** The line under the title of a card: the step the card waits at and since when. */
export function stageText(key: BoardColumnKey, item: BoardItem, now: Date = new Date()): string {
  const app = item.application
  switch (key) {
    case 'new':
      return isToday(item.stage_at, now) ? 'новая сегодня' : `найдена ${ago(item.stage_at, now)}`
    case 'confirm':
      if (app?.status === 'queued') return 'в очереди на отправку'
      if (app?.status === 'sending') return 'отправляется'
      return `открыт ${ago(item.stage_at, now)}`
    case 'sent':
      return `ждёт просмотра ${waiting(item.stage_at, now)}`
    case 'viewed':
      return `смотрели ${ago(item.stage_at, now)}`
    case 'answer':
      return `ответили ${ago(item.stage_at, now)}`
  }
}

const OUTCOME: Record<NonNullable<BoardItem['outcome']>, { label: string; tone: BadgeTone }> = {
  invitation: { label: 'Приглашение', tone: 'good' },
  discard: { label: 'Отказ', tone: 'bad' },
  message: { label: 'Сообщение', tone: 'info' },
}

export function outcomeBadge(outcome: BoardItem['outcome']): { label: string; tone: BadgeTone } | null {
  return outcome ? OUTCOME[outcome] : null
}

/** A sent response whose employer side the user records by hand: hh.ru does not sync it (the manual journal). */
export function manualJournal(app: ApplicationRow | null): boolean {
  return Boolean(app && app.status === 'sent' && (!app.hh_negotiation_id || app.hh_negotiation_id.startsWith('manual-')))
}

/** Why the response to a new vacancy cannot be opened yet: the blocking problems of its letter (a [[УТОЧНИТЬ]] marker,
 *  a link, the length) or a required letter that is missing; '' when it can. The rule of the side card and the server:
 *  Claude's questions alone do not block, the letter is whole without the answers. */
export function sendBlock(d: Pick<VacancyDetail, 'letter' | 'response_letter_required'>): string {
  return d.letter?.send_problems.join('; ') || (d.response_letter_required && !d.letter ? 'работодатель требует письмо, а его ещё нет' : '')
}

export type BoardAction = 'open' | 'queue' | 'confirm' | 'viewed' | 'card'

/** The next step of a card: Enter and the main button of its row. */
export function primaryAction(key: BoardColumnKey, item: BoardItem, mode: 'api' | 'assist'): { action: BoardAction; label: string } | null {
  const app = item.application
  switch (key) {
    case 'new':
      return mode === 'api' ? { action: 'queue', label: 'Откликнуться через API' } : { action: 'open', label: 'Открыть на hh.ru' }
    case 'confirm':
      return app?.mode === 'assist' && app.status === 'needs_manual' ? { action: 'confirm', label: 'Да, отправлено' } : null
    case 'sent':
      return manualJournal(app) ? { action: 'viewed', label: 'Смотрели' } : { action: 'card', label: 'Карточка отклика' }
    case 'viewed':
      return { action: 'card', label: manualJournal(app) ? 'Записать ответ' : 'Карточка отклика' }
    case 'answer':
      return { action: 'card', label: 'Карточка отклика' }
  }
}

/** The keys of the open column, in words, for the line at its foot. */
export function keyHint(key: BoardColumnKey, mode: 'api' | 'assist'): string {
  const walk = 'J/K или ↑↓ ходят'
  const tail = '← → колонки · Esc вся доска'
  switch (key) {
    case 'new':
      return `${walk} · Enter: ${mode === 'api' ? 'откликнуться через API' : 'открыть на hh.ru'} · S пропустить · D отложить · ${tail}`
    case 'confirm':
      return `${walk} · Enter: да, отправлено · ${tail}`
    case 'sent':
      return `${walk} · Enter: смотрели · ${tail}`
    default:
      return `${walk} · Enter: карточка отклика · ${tail}`
  }
}

const EMPTY: Record<BoardColumnKey, [string, string]> = {
  new: ['Решать нечего', 'Новые вакансии появятся после проверки поисков и разбора в Claude.'],
  confirm: ['Подтверждать нечего', 'Здесь ждут отклики, которые вы открыли на hh.ru, но ещё не отметили отправленными.'],
  sent: ['Все отклики просмотрены', 'Здесь лежат отправленные отклики, которые работодатель ещё не открыл.'],
  viewed: ['Пока никто не смотрел', 'Сюда попадают отклики, которые работодатель открыл, но ещё не ответил.'],
  answer: ['Ответов пока нет', 'Приглашения, отказы и сообщения работодателей будут здесь.'],
}

const EMPTY_BAND: Partial<Record<BoardBand, [string, string]>> = {
  clarify: ['Уточнять нечего', 'Во всех письмах, которые ещё могут уйти, нет вопросов к вам.'],
  low: ['Отклонённых нет', 'Здесь будут вакансии, которые Claude оценил ниже порога.'],
  wait: ['Очередь Claude пуста', 'Все найденные вакансии уже разобраны.'],
  manual: ['Действий на hh.ru не нужно', 'Здесь будут вакансии, которым нужен шаг на сайте, пока отклик не открыт.'],
}

/** What an empty open column says: the reason and what will come there. */
export function emptyText(key: BoardColumnKey, band: BoardBand | null): [string, string] {
  return (key === 'new' && band && EMPTY_BAND[band]) || EMPTY[key]
}
