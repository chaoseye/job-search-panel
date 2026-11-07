import { describe, expect, it } from 'vitest'
import { DEFAULT_FILTERS, FILTERS_KEY, isGroup, LAYOUT_KEY, loadFilters, loadLayout, savedScopeQuery, scopeQuery } from './vacancyFilters'

function storage(value: string | null) {
  return { getItem: (key: string) => (key === FILTERS_KEY ? value : null) }
}

describe('loadFilters', () => {
  it('keeps valid saved values', () => {
    const saved = { group: 'maybe', q: 'python', sort: 'published', unseen_only: true, min_score: '60' }
    expect(loadFilters(storage(JSON.stringify(saved)))).toEqual({ ...DEFAULT_FILTERS, ...saved })
  })

  it('drops what the API would refuse or the page cannot render', () => {
    // {"q": null} blanked the tab, the old sort «date» turned the list into a 422
    const saved = { q: null, group: 'nope', sort: 'date', min_score: 'abc', include_snoozed: 'yes', area: 'Москва', bogus: 1 }
    expect(loadFilters(storage(JSON.stringify(saved)))).toEqual({ ...DEFAULT_FILTERS, area: 'Москва' })
  })

  it.each(['', 'null', '[1, 2]', '"text"', '42', '{broken'])('falls back to the defaults on %s', (raw) => {
    expect(loadFilters(storage(raw))).toEqual(DEFAULT_FILTERS)
  })

  it('survives a storage that throws (private mode)', () => {
    const throwing = {
      getItem: () => {
        throw new Error('SecurityError')
      },
    }
    expect(loadFilters(throwing)).toEqual(DEFAULT_FILTERS)
  })
})

describe('the freshness filter in the saved filters', () => {
  it('is on with 5 days by default, also for filters saved before it existed', () => {
    expect(DEFAULT_FILTERS.fresh_days).toBe('5')
    expect(loadFilters(storage(JSON.stringify({ group: 'maybe', q: 'go' }))).fresh_days).toBe('5')
    expect(savedScopeQuery(storage(null))).toBe('fresh_days=5')
  })

  it('stays off once turned off', () => {
    expect(loadFilters(storage(JSON.stringify({ fresh_days: '' }))).fresh_days).toBe('')
    expect(savedScopeQuery(storage(JSON.stringify({ fresh_days: '' })))).toBe('')
    expect(savedScopeQuery(storage(JSON.stringify({ fresh_days: '14' })))).toBe('fresh_days=14')
  })

  it.each(['0', '-1', '1.5', '366', '05', 'abc'])('drops %s: the API takes 1–365 days', (raw) => {
    expect(loadFilters(storage(JSON.stringify({ fresh_days: raw }))).fresh_days).toBe('5')
  })

  it('drops a value of another type', () => {
    expect(loadFilters(storage(JSON.stringify({ fresh_days: 5 }))).fresh_days).toBe('5')
    expect(loadFilters(storage(JSON.stringify({ fresh_days: false }))).fresh_days).toBe('5')
  })
})

describe('the responses cap in the saved filters', () => {
  it('is off by default, also for filters saved before it existed', () => {
    expect(DEFAULT_FILTERS.max_responses).toBe('')
    expect(loadFilters(storage(JSON.stringify({ group: 'maybe', fresh_days: '' }))).max_responses).toBe('')
  })

  it('keeps the number set, 0 and 999 999 too, and the list and the menu ask with it', () => {
    expect(loadFilters(storage(JSON.stringify({ max_responses: '1000' }))).max_responses).toBe('1000')
    expect(loadFilters(storage(JSON.stringify({ max_responses: '0' }))).max_responses).toBe('0')
    expect(savedScopeQuery(storage(JSON.stringify({ max_responses: '1000' })))).toBe('fresh_days=5&max_responses=1000')
    expect(savedScopeQuery(storage(JSON.stringify({ fresh_days: '', max_responses: '999999' })))).toBe('max_responses=999999')
  })

  it.each(['-1', '1.5', '1000000', '01000', ' 100', '1 000', 'abc'])('drops %s: the API takes 0–999 999, the field types no leading zeros', (raw) => {
    expect(loadFilters(storage(JSON.stringify({ max_responses: raw }))).max_responses).toBe('')
  })

  it('drops a value of another type', () => {
    expect(loadFilters(storage(JSON.stringify({ max_responses: 1000 }))).max_responses).toBe('')
    expect(loadFilters(storage(JSON.stringify({ max_responses: null }))).max_responses).toBe('')
  })
})

describe('scopeQuery', () => {
  it('has the standing filters that are on and nothing else', () => {
    expect(scopeQuery({ fresh_days: '', max_responses: '' })).toBe('')
    expect(scopeQuery({ fresh_days: '5', max_responses: '' })).toBe('fresh_days=5')
    expect(scopeQuery({ fresh_days: '5', max_responses: '0' })).toBe('fresh_days=5&max_responses=0')
  })
})

describe('loadLayout', () => {
  const layoutStorage = (value: string | null) => ({ getItem: (key: string) => (key === LAYOUT_KEY ? value : null) })

  it('keeps the classic view once chosen', () => {
    expect(loadLayout(layoutStorage('classic'))).toBe('classic')
  })

  it.each([null, '', 'compact', 'Classic', 'table'])('falls back to the compact view on %s', (raw) => {
    expect(loadLayout(layoutStorage(raw))).toBe('compact')
  })

  it('survives a storage that throws (private mode)', () => {
    expect(
      loadLayout({
        getItem: () => {
          throw new Error('SecurityError')
        },
      }),
    ).toBe('compact')
  })
})

describe('isGroup', () => {
  it('knows the groups of the tab', () => {
    expect(isGroup('needs_clarification')).toBe(true)
    expect(isGroup('drop table')).toBe(false)
    expect(isGroup(null)).toBe(false)
  })
})
