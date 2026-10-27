import { describe, expect, it } from 'vitest'
import type { ApplicationRow, BoardItem, BoardNewCounts } from '../api/types'
import { ago, bandChips, defaultBand, funnelCells, isBand, isColumnKey, keyHint, manualJournal, neighbourColumn, primaryAction, stageText } from './board'

const NOW = new Date('2026-10-24T15:00:00Z')
const T = { suitable: 70, maybe: 50 }
const COUNTS: BoardNewCounts = { hi: 14, mid: 77, low: 372, wait: 0, manual: 0, fresh_today: 2, clarify: 36, snoozed: 1 }

function app(patch: Partial<ApplicationRow> = {}): ApplicationRow {
  return { id: 5, vacancy_id: 1, mode: 'assist', status: 'sent', hh_negotiation_id: null, ...patch } as ApplicationRow
}

function item(patch: Partial<BoardItem> = {}): BoardItem {
  return { vacancy: {} as BoardItem['vacancy'], application: null, stage_at: '2026-10-21T15:00:00Z', outcome: null, ...patch }
}

describe('board model', () => {
  it('knows its columns and bands and walks between the columns', () => {
    expect(isColumnKey('confirm')).toBe(true)
    expect(isColumnKey('elsewhere')).toBe(false)
    expect(isBand('clarify')).toBe(true)
    expect(isBand(null)).toBe(false)
    expect(neighbourColumn('new', -1)).toBeNull()
    expect(neighbourColumn('new', 1)).toBe('confirm')
    expect(neighbourColumn('answer', 1)).toBeNull()
  })

  it('shows the decision bands always and the others while they have cards or are open', () => {
    expect(bandChips(COUNTS, T, 'hi').map((c) => [c.label, c.count])).toEqual([
      ['От 70', 14],
      ['50–69', 77],
      ['Уточнить письмо', 36],
      ['Ниже 50', 372],
    ])
    expect(bandChips({ ...COUNTS, clarify: 0 }, T, 'clarify').map((c) => c.band)).toEqual(['hi', 'mid', 'clarify', 'low'])
    expect(bandChips({ ...COUNTS, wait: 3, manual: 1 }, T, null).filter((c) => c.attention).map((c) => c.band)).toEqual(['clarify', 'manual'])
    expect(defaultBand(COUNTS)).toBe('hi')
    expect(defaultBand({ ...COUNTS, hi: 0 })).toBe('mid')
    expect(defaultBand({ ...COUNTS, hi: 0, mid: 0, manual: 2 })).toBe('manual')
  })

  it('finds the weakest step of the funnel past the first one', () => {
    const cells = funnelCells([
      { key: 'found', label: 'найдено', value: 1065, hint: null },
      { key: 'good', label: 'от 50', value: 167, hint: null },
      { key: 'sent', label: 'отправлено', value: 128, hint: null },
      { key: 'viewed', label: 'смотрели', value: 15, hint: null },
      { key: 'invited', label: 'приглашений', value: 2, hint: null },
    ])
    expect(cells.map((c) => c.conv)).toEqual([null, 16, 77, 12, 13])
    expect(cells.map((c) => c.weak)).toEqual([false, false, false, true, false])
    // nothing sent: no conversion after an empty step, no weak step to point at
    const empty = funnelCells([
      { key: 'found', label: 'a', value: 10, hint: null },
      { key: 'good', label: 'b', value: 0, hint: null },
      { key: 'sent', label: 'c', value: 0, hint: null },
    ])
    expect(empty.map((c) => [c.conv, c.weak])).toEqual([
      [null, false],
      [0, false],
      [null, false],
    ])
  })

  it('says where a card stands and since when', () => {
    expect(ago('2026-10-24T12:00:00Z', NOW)).toBe('3 ч назад')
    expect(ago('2026-10-24T15:00:00Z', NOW)).toBe('только что')
    expect(ago('2026-09-01T12:00:00Z', NOW)).toBe('01.09')
    expect(stageText('new', item({ stage_at: '2026-10-24T09:00:00Z' }), NOW)).toBe('новая сегодня')
    expect(stageText('new', item(), NOW)).toBe('найдена 3 дн. назад')
    expect(stageText('confirm', item({ application: app({ status: 'needs_manual' }) }), NOW)).toBe('открыт 3 дн. назад')
    expect(stageText('confirm', item({ application: app({ mode: 'api', status: 'queued' }) }), NOW)).toBe('в очереди на отправку')
    expect(stageText('sent', item(), NOW)).toBe('ждёт просмотра 3 дн.')
    expect(stageText('sent', item({ stage_at: '2026-09-01T12:00:00Z' }), NOW)).toBe('ждёт просмотра с 01.09')
    expect(stageText('viewed', item(), NOW)).toBe('смотрели 3 дн. назад')
    expect(stageText('answer', item(), NOW)).toBe('ответили 3 дн. назад')
  })

  it('picks the next step of a card for the mode and the journal', () => {
    expect(primaryAction('new', item(), 'assist')).toEqual({ action: 'open', label: 'Открыть на hh.ru' })
    expect(primaryAction('new', item(), 'api')?.action).toBe('queue')
    expect(primaryAction('confirm', item({ application: app({ status: 'needs_manual' }) }), 'assist')?.label).toBe('Да, отправлено')
    expect(primaryAction('confirm', item({ application: app({ mode: 'api', status: 'queued' }) }), 'api')).toBeNull()
    // hh.ru syncs this one: nothing to record by hand
    expect(primaryAction('sent', item({ application: app({ hh_negotiation_id: '123456' }) }), 'assist')?.action).toBe('card')
    expect(primaryAction('sent', item({ application: app({ hh_negotiation_id: 'manual-5' }) }), 'assist')?.action).toBe('viewed')
    expect(primaryAction('viewed', item({ application: app() }), 'assist')?.label).toBe('Записать ответ')
    expect(manualJournal(app({ status: 'needs_manual' }))).toBe(false)
    expect(keyHint('new', 'assist')).toContain('Enter: открыть на hh.ru')
    expect(keyHint('sent', 'assist')).toContain('Enter: смотрели')
  })
})
