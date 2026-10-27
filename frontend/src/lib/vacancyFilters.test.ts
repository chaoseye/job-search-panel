import { describe, expect, it } from 'vitest'
import { DEFAULT_FILTERS, FILTERS_KEY, isGroup, LAYOUT_KEY, loadFilters, loadLayout } from './vacancyFilters'

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
