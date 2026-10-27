import { describe, expect, it } from 'vitest'
import type { StudioBoardItem, StudioRow, TenderBoardItem, TenderRow } from '../api/types'
import {
  contactsText,
  isSource,
  neighbourIn,
  shortDay,
  STUDIO_COLUMNS,
  studioDefaultBand,
  studioEmpty,
  studioMeta,
  studioPrimary,
  studioSendTarget,
  studioStageText,
  telegramHref,
  TENDER_COLUMNS,
  tenderDefaultBand,
  tenderEmpty,
  tenderMeta,
  tenderPrimary,
  tenderStageText,
} from './boardSources'

const NOW = new Date('2026-10-25T15:00:00Z')

function tender(patch: Partial<TenderRow> = {}): TenderRow {
  return { id: 1, title: 'Сайт', status: 'new', open: true, deadline_on: '2026-11-05', budget: null, ...patch } as TenderRow
}

function studio(patch: Partial<StudioRow> = {}): StudioRow {
  return { id: 1, name: 'Пиксель', status: 'ready', emails: [], telegram: null, ...patch } as StudioRow
}

const tItem = (patch: Partial<TenderRow> = {}, stage_at = '2026-10-22T15:00:00Z'): TenderBoardItem => ({ tender: tender(patch), stage_at })
const sItem = (patch: Partial<StudioRow> = {}, stage_at = '2026-10-22T15:00:00Z'): StudioBoardItem => ({ studio: studio(patch), stage_at })

describe('board sources', () => {
  it('knows the sources and walks the columns of each', () => {
    expect(isSource('tenders')).toBe(true)
    expect(isSource('channels')).toBe(false)
    expect(isSource(null)).toBe(false)
    expect(neighbourIn(TENDER_COLUMNS, 'new', 1)).toBe('ready')
    expect(neighbourIn(TENDER_COLUMNS, 'responded', 1)).toBe('answer')
    expect(neighbourIn(TENDER_COLUMNS, 'answer', 1)).toBeNull()
    expect(neighbourIn(STUDIO_COLUMNS, 'sent', -1)).toBe('ready')
    expect(shortDay('2026-11-05')).toBe('05.11')
    expect(shortDay(null)).toBe('—')
  })

  it('tenders: the band to open, the line under a card, the two steps of posting an answer', () => {
    expect(tenderDefaultBand({ relevant: 3, other: 9 })).toBe('relevant')
    expect(tenderDefaultBand({ relevant: 0, other: 9 })).toBe('other')
    expect(tenderDefaultBand(undefined)).toBe('relevant')

    expect(tenderStageText('new', tItem({}, '2026-10-25T09:00:00Z'), NOW)).toBe('найден сегодня')
    expect(tenderStageText('new', tItem(), NOW)).toBe('найден 3 дн. назад')
    expect(tenderStageText('ready', tItem({ status: 'drafting' }), NOW)).toBe('Claude готовит отклик')
    expect(tenderStageText('ready', tItem({ status: 'drafted' }), NOW)).toBe('заявки до 05.11')
    expect(tenderStageText('ready', tItem({ status: 'drafted', open: false }), NOW)).toBe('приём заявок закрыт')
    expect(tenderStageText('responded', tItem({ status: 'responded' }), NOW)).toBe('откликнулся 3 дн. назад')
    expect(tenderStageText('answer', tItem({ status: 'replied' }), NOW)).toBe('ответили 3 дн. назад')
    expect(tenderStageText('answer', tItem({ status: 'declined' }), NOW)).toBe('отказ 3 дн. назад')

    expect(tenderPrimary('new', tender(), false)).toEqual({ action: 'draft', label: 'Подготовить отклик' })
    expect(tenderPrimary('ready', tender({ status: 'drafting' }), false)).toBeNull()
    expect(tenderPrimary('ready', tender({ status: 'drafted' }), false)).toEqual({ action: 'open', label: 'Открыть на workspace.ru' })
    expect(tenderPrimary('ready', tender({ status: 'drafted' }), true)).toEqual({ action: 'responded', label: 'Да, откликнулся' })
    // a closed tender cannot be answered any more: it leaves the board
    expect(tenderPrimary('ready', tender({ status: 'drafted', open: false }), false)?.action).toBe('skip')
    // after responding the organizer's answer is recorded by hand
    expect(tenderPrimary('responded', tender({ status: 'responded' }), false)).toEqual({ action: 'replied', label: 'Ответили' })
    expect(tenderPrimary('answer', tender({ status: 'declined' }), false)?.action).toBe('card')

    expect(tenderMeta({ relevant: 19, other: 31 }, 4)).toBe('19 подходящих тендеров открыто · откликов в работе: 4')
    expect(tenderMeta({ relevant: 1, other: 0 }, 0)).toBe('1 подходящий тендер открыт')
    expect(tenderEmpty('new', 'other')[0]).toBe('Других открытых тендеров нет')
    expect(tenderEmpty('ready', null)[0]).toBe('Готовых откликов нет')
    expect(tenderEmpty('answer', null)[0]).toBe('Ответов пока нет')
  })

  it('studios: contacts decide the band and the step, a letter opens first and is marked sent after', () => {
    expect(studioDefaultBand({ contacts: 5, unchecked: 2, nocontacts: 1 })).toBe('contacts')
    expect(studioDefaultBand({ contacts: 0, unchecked: 2, nocontacts: 1 })).toBe('unchecked')
    expect(studioDefaultBand({ contacts: 0, unchecked: 0, nocontacts: 1 })).toBe('contacts')

    const mail = studio({ emails: ['a@b.example', 'c@d.example'], telegram: '@example_pixel' })
    const tg = studio({ telegram: 't.me/example_pixel' })
    expect(contactsText(mail)).toBe('почта: 2 · Telegram')
    expect(studioSendTarget(mail)).toEqual({ to: 'a@b.example', via: 'email' })
    expect(studioSendTarget(tg)).toEqual({ to: 't.me/example_pixel', via: 'telegram' })
    expect(studioSendTarget(studio())).toBeNull()
    expect(telegramHref('@example_pixel')).toBe('https://t.me/example_pixel')
    expect(telegramHref('t.me/example_pixel')).toBe('https://t.me/example_pixel')
    expect(telegramHref('https://t.me/example_pixel')).toBe('https://t.me/example_pixel')

    expect(studioStageText('new', sItem({ status: 'new' }), NOW)).toBe('контакты не проверены')
    expect(studioStageText('new', sItem({ emails: ['a@b.example'] }), NOW)).toBe('почта: 1')
    expect(studioStageText('new', sItem({ status: 'no_contacts' }), NOW)).toBe('контактов не нашлось')
    expect(studioStageText('ready', sItem({ status: 'drafting' }), NOW)).toBe('Claude пишет письмо')
    expect(studioStageText('ready', sItem({ status: 'drafted' }), NOW)).toBe('письмо готово 3 дн. назад')
    expect(studioStageText('answer', sItem({ status: 'declined' }), NOW)).toBe('отказ')

    expect(studioPrimary('new', studio(), false)?.action).toBe('letter')
    expect(studioPrimary('new', studio({ status: 'new' }), false)?.action).toBe('contacts')
    expect(studioPrimary('new', studio({ status: 'no_contacts' }), false)).toEqual({ action: 'card', label: 'Вписать контакты' })
    expect(studioPrimary('ready', studio({ status: 'drafting' }), false)).toBeNull()
    expect(studioPrimary('ready', { ...mail, status: 'drafted' }, false)).toEqual({ action: 'mail', label: 'Открыть в почте' })
    expect(studioPrimary('ready', { ...tg, status: 'drafted' }, false)).toEqual({ action: 'telegram', label: 'Открыть Telegram' })
    expect(studioPrimary('ready', { ...mail, status: 'drafted' }, true)).toEqual({ action: 'sent', label: 'Да, отправлено' })
    expect(studioPrimary('ready', studio({ status: 'drafted' }), false)?.action).toBe('card')
    expect(studioPrimary('sent', studio({ status: 'sent' }), false)?.action).toBe('replied')
    expect(studioPrimary('answer', studio({ status: 'replied' }), false)?.action).toBe('card')

    expect(studioMeta({ contacts: 252, unchecked: 1, nocontacts: 32 }, 42, 3, 20)).toBe('252 студии с контактами ждут письма · писем в работе: 42 · отправлено сегодня 3 из 20')
    expect(studioEmpty('new', 'nocontacts')[0]).toBe('Таких студий нет')
    expect(studioEmpty('answer', null)[0]).toBe('Ответов пока нет')
  })
})
