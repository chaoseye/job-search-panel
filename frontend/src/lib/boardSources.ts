// The boards of the other pipelines of «Воронка»: tenders of workspace.ru and studios. What their columns
// and bands are called, the line under a card, its next step, the keys and the empty columns. Pure functions; the
// board of vacancies lives in lib/board.ts.
import type {
  BoardSource,
  StudioBoardBand,
  StudioBoardColumnKey,
  StudioBoardItem,
  StudioRow,
  TenderBoardBand,
  TenderBoardColumnKey,
  TenderBoardItem,
  TenderRow,
} from '../api/types'
import { ago } from './board'
import { countOf } from './format'

export const SOURCES: [BoardSource, string][] = [
  ['vacancies', 'Вакансии'],
  ['tenders', 'Тендеры'],
  ['studios', 'Студии'],
]

export function isSource(value: string | null | undefined): value is BoardSource {
  return SOURCES.some(([s]) => s === value)
}

/** The column to the left (-1) or to the right (1) of `key` among `keys`, null at the edge. */
export function neighbourIn<K extends string>(keys: readonly K[], key: K, step: 1 | -1): K | null {
  return keys[keys.indexOf(key) + step] ?? null
}

function isToday(iso: string | null | undefined, now: Date): boolean {
  return Boolean(iso) && new Date(iso as string).toDateString() === now.toDateString()
}

// ---- tenders ---------------------------------------------------------------------------------------------------------

export const TENDER_COLUMNS: TenderBoardColumnKey[] = ['new', 'ready', 'responded', 'answer']
export const TENDER_BANDS: TenderBoardBand[] = ['relevant', 'other']

export function isTenderColumn(value: string | null | undefined): value is TenderBoardColumnKey {
  return TENDER_COLUMNS.includes(value as TenderBoardColumnKey)
}

export function isTenderBand(value: string | null | undefined): value is TenderBoardBand {
  return TENDER_BANDS.includes(value as TenderBoardBand)
}

export function tenderBandLabel(band: TenderBoardBand): string {
  return band === 'relevant' ? 'Подходят' : 'Остальные открытые'
}

/** The band «Найдено» opens on: the suitable tenders, the other open ones when there are no suitable ones. */
export function tenderDefaultBand(bands: Record<TenderBoardBand, number> | undefined): TenderBoardBand {
  return bands && bands.relevant === 0 && bands.other > 0 ? 'other' : 'relevant'
}

/** «2026-11-05» → «05.11». */
export function shortDay(iso: string | null | undefined): string {
  if (!iso) return '—'
  const [, m, d] = iso.split('-')
  return `${d}.${m}`
}

/** The line under the title of a tender's card: the step it waits at, the deadline of an answer to post. */
export function tenderStageText(key: TenderBoardColumnKey, item: TenderBoardItem, now: Date = new Date()): string {
  const t = item.tender
  switch (key) {
    case 'new':
      return isToday(item.stage_at, now) ? 'найден сегодня' : `найден ${ago(item.stage_at, now)}`
    case 'ready':
      if (t.status === 'drafting') return 'Claude готовит отклик'
      return t.open ? `заявки до ${shortDay(t.deadline_on)}` : 'приём заявок закрыт'
    case 'responded':
      return `откликнулся ${ago(item.stage_at, now)}`
    case 'answer':
      return `${t.status === 'declined' ? 'отказ' : 'ответили'} ${ago(item.stage_at, now)}`
  }
}

export type TenderAction = 'draft' | 'open' | 'responded' | 'replied' | 'skip' | 'card'

/** The next step of a tender's card: Enter and the main button of its row. An answer to post opens workspace.ru first
 *  (the answer copied), then asks «Да, откликнулся»; an answer Claude still writes has no step; after responding the
 *  organizer's answer is recorded by hand («Ответили», a refusal by its own button). */
export function tenderPrimary(key: TenderBoardColumnKey, t: TenderRow, opened: boolean): { action: TenderAction; label: string } | null {
  switch (key) {
    case 'new':
      return { action: 'draft', label: 'Подготовить отклик' }
    case 'ready':
      if (t.status === 'drafting') return null
      if (!t.open) return { action: 'skip', label: 'Пропустить' }
      return opened ? { action: 'responded', label: 'Да, откликнулся' } : { action: 'open', label: 'Открыть на workspace.ru' }
    case 'responded':
      return { action: 'replied', label: 'Ответили' }
    case 'answer':
      return { action: 'card', label: 'Карточка тендера' }
  }
}

const WALK = 'J/K или ↑↓ ходят'
const TAIL = '← → колонки · Esc вся доска'

export function tenderKeyHint(key: TenderBoardColumnKey): string {
  switch (key) {
    case 'new':
      return `${WALK} · Enter: подготовить отклик · S пропустить · ${TAIL}`
    case 'ready':
      return `${WALK} · Enter: открыть на workspace.ru, потом «Да, откликнулся» · S пропустить · ${TAIL}`
    case 'responded':
      return `${WALK} · Enter: ответили · ${TAIL}`
    case 'answer':
      return `${WALK} · Enter: карточка тендера · ${TAIL}`
  }
}

export function tenderEmpty(key: TenderBoardColumnKey, band: TenderBoardBand | null): [string, string] {
  if (key === 'new' && band === 'other') return ['Других открытых тендеров нет', 'Здесь будут открытые тендеры, которые не совпали с анкетой и разделами.']
  switch (key) {
    case 'new':
      return ['Подходящих тендеров нет', 'Монитор читает workspace.ru раз в час. Остальные открытые тендеры в соседней группе.']
    case 'ready':
      return ['Готовых откликов нет', 'Нажмите «Подготовить отклик» у найденного тендера: Claude напишет его по тексту тендера.']
    case 'responded':
      return ['Откликов без ответа нет', 'Здесь тендеры, на которые вы откликнулись на workspace.ru, пока заказчик не ответил.']
    case 'answer':
      return ['Ответов пока нет', 'Ответ или отказ заказчика отметьте у тендера в колонке «Откликнулся».']
  }
}

export function tenderMeta(bands: Record<TenderBoardBand, number>, ready: number): string {
  return [
    countOf(bands.relevant, ['подходящий тендер открыт', 'подходящих тендера открыто', 'подходящих тендеров открыто']),
    ready > 0 ? `откликов в работе: ${ready}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
}

// ---- studios ---------------------------------------------------------------------------------------------------------

export const STUDIO_COLUMNS: StudioBoardColumnKey[] = ['new', 'ready', 'sent', 'answer']
export const STUDIO_BANDS: StudioBoardBand[] = ['contacts', 'unchecked', 'nocontacts']

export function isStudioColumn(value: string | null | undefined): value is StudioBoardColumnKey {
  return STUDIO_COLUMNS.includes(value as StudioBoardColumnKey)
}

export function isStudioBand(value: string | null | undefined): value is StudioBoardBand {
  return STUDIO_BANDS.includes(value as StudioBoardBand)
}

export function studioBandLabel(band: StudioBoardBand): string {
  return band === 'contacts' ? 'Есть контакты' : band === 'unchecked' ? 'Контакты не проверены' : 'Контактов не нашлось'
}

/** The band «Найдено» opens on: the studios with contacts, then the ones not checked yet. */
export function studioDefaultBand(bands: Record<StudioBoardBand, number> | undefined): StudioBoardBand {
  if (!bands || bands.contacts > 0) return 'contacts'
  return bands.unchecked > 0 ? 'unchecked' : 'contacts'
}

/** «почта: 2 · Telegram». */
export function contactsText(s: StudioRow): string {
  return [s.emails.length ? `почта: ${s.emails.length}` : null, s.telegram ? 'Telegram' : null].filter(Boolean).join(' · ')
}

/** The line under the title of a studio's card: its contacts, the letter, the sending, the answer. */
export function studioStageText(key: StudioBoardColumnKey, item: StudioBoardItem, now: Date = new Date()): string {
  const s = item.studio
  switch (key) {
    case 'new':
      if (s.status === 'new') return 'контакты не проверены'
      return contactsText(s) || 'контактов не нашлось'
    case 'ready':
      return s.status === 'drafting' ? 'Claude пишет письмо' : `письмо готово ${ago(item.stage_at, now)}`
    case 'sent':
      return `отправлено ${ago(item.stage_at, now)}`
    case 'answer':
      return s.status === 'declined' ? 'отказ' : `ответили ${ago(item.stage_at, now)}`
  }
}

/** Where the letter goes and how: the first e-mail, else Telegram; null when there is nowhere to send it. */
export function studioSendTarget(s: StudioRow): { to: string; via: 'email' | 'telegram' } | null {
  if (s.emails[0]) return { to: s.emails[0], via: 'email' }
  if (s.telegram) return { to: s.telegram, via: 'telegram' }
  return null
}

/** «@name», «t.me/name», «https://t.me/name» → https://t.me/name. */
export function telegramHref(handle: string): string {
  const h = handle.trim()
  if (/^https?:\/\//i.test(h)) return h
  if (/^t\.me\//i.test(h)) return `https://${h}`
  return `https://t.me/${h.replace(/^@/, '')}`
}

export type StudioAction = 'letter' | 'contacts' | 'mail' | 'telegram' | 'sent' | 'replied' | 'card'

/** The next step of a studio's card: Enter and the main button of its row. A letter opens in the mail program (or
 *  Telegram, the letter copied) first, then asks «Да, отправлено»; a letter Claude still writes has no step. */
export function studioPrimary(key: StudioBoardColumnKey, s: StudioRow, opened: boolean): { action: StudioAction; label: string } | null {
  switch (key) {
    case 'new':
      if (s.status === 'ready') return { action: 'letter', label: 'Подготовить письмо' }
      if (s.status === 'new') return { action: 'contacts', label: 'Проверить контакты' }
      return { action: 'card', label: 'Вписать контакты' }
    case 'ready': {
      if (s.status === 'drafting') return null
      if (opened) return { action: 'sent', label: 'Да, отправлено' }
      const target = studioSendTarget(s)
      if (target?.via === 'email') return { action: 'mail', label: 'Открыть в почте' }
      if (target?.via === 'telegram') return { action: 'telegram', label: 'Открыть Telegram' }
      return { action: 'card', label: 'Вписать контакты' }
    }
    case 'sent':
      return { action: 'replied', label: 'Ответили' }
    case 'answer':
      return { action: 'card', label: 'Карточка студии' }
  }
}

export function studioKeyHint(key: StudioBoardColumnKey): string {
  switch (key) {
    case 'new':
      return `${WALK} · Enter: подготовить письмо · S пропустить · ${TAIL}`
    case 'ready':
      return `${WALK} · Enter: открыть письмо, потом «Да, отправлено» · S пропустить · ${TAIL}`
    case 'sent':
      return `${WALK} · Enter: ответили · ${TAIL}`
    case 'answer':
      return `${WALK} · Enter: карточка студии · ${TAIL}`
  }
}

export function studioEmpty(key: StudioBoardColumnKey, band: StudioBoardBand | null): [string, string] {
  if (key === 'new' && band === 'unchecked') return ['Все контакты проверены', 'Здесь будут студии, сайт которых панель ещё не читала.']
  if (key === 'new' && band === 'nocontacts') return ['Таких студий нет', 'Здесь студии, на сайте которых не нашлось почты и Telegram. Контакты можно вписать в карточке.']
  switch (key) {
    case 'new':
      return ['Студий с контактами нет', 'Найдите студии на странице «Студии» или проверьте контакты найденных.']
    case 'ready':
      return ['Готовых писем нет', 'Нажмите «Подготовить письмо» у студии с контактами: Claude напишет два варианта.']
    case 'sent':
      return ['Отправленных нет', 'Здесь письма, которые вы отправили и отметили, пока студия не ответила.']
    case 'answer':
      return ['Ответов пока нет', 'Отметьте ответ сами или подключите почту в настройках: ответы студий найдутся в ней.']
  }
}

export function studioMeta(bands: Record<StudioBoardBand, number>, ready: number, sentToday: number, limit: number): string {
  return [
    countOf(bands.contacts, ['студия с контактами ждёт письма', 'студии с контактами ждут письма', 'студий с контактами ждут письма']),
    ready > 0 ? `писем в работе: ${ready}` : null,
    `отправлено сегодня ${sentToday} из ${limit}`,
  ]
    .filter(Boolean)
    .join(' · ')
}
