// Filters of the «Вакансии» page: the groups, the defaults and the saved state (localStorage). Kept out of the
// page so the parsing of a saved (possibly stale or hand-edited) state is testable on its own.

export const GROUPS: [string, string][] = [
  ['candidates', 'Кандидаты'],
  ['suitable', 'Подходят'],
  ['maybe', 'Возможно'],
  ['prescored', 'Ждут разбора'],
  ['needs_manual', 'Нужно действие на hh'],
  ['not_suitable', 'Ниже порога'],
  ['filtered_out', 'Отфильтрованы правилами'],
  ['skipped', 'Пропущенные'],
  ['hidden', 'Скрытые'],
  ['already_applied', 'Уже есть отклик'],
  ['queued', 'В очереди на отклик'],
  ['sent', 'Отклик отправлен'],
  ['needs_clarification', 'Уточнить в письме'],
  ['new', 'Не обработаны'],
  ['watched', 'Наблюдение'],
  ['all', 'Все'],
]

export interface Filters {
  group: string
  min_score: string
  search_id: string
  selection_profile_id: string
  area: string
  work_format: string
  experience: string
  salary_from: string
  q: string
  include_snoozed: boolean
  unseen_only: boolean
  sort: string
  /** The freshness filter: outside the cities of the questionnaire only vacancies published within so many days;
   *  '' — off. On by default, so a browser with filters saved before it gets it too. */
  fresh_days: string
  /** The responses cap: only vacancies with at most so many responses on hh.ru; '' — off, the default (the user sets
   *  the number). */
  max_responses: string
  /** The site of the vacancy (after HireHi became the second source): '' — every site, `hh`, `hirehi`. Kept
   *  with the other filters and shown in the address too (?source=hirehi): a link with it opens the list of that site. */
  source: string
}

/** The sites of the filter «Сайт», '' — all of them; the values are `source` of the API. */
export const SITES: [string, string][] = [
  ['', 'Все сайты'],
  ['hh', 'hh.ru'],
  ['hirehi', 'HireHi'],
]

export function isSite(value: unknown): value is string {
  return typeof value === 'string' && value !== '' && SITES.some(([id]) => id === value)
}

/** The filters with the site a link names (/vacancies?source=hirehi): it wins over the saved one; an unknown value is
 *  ignored (the API would answer 422). */
export function withSiteFrom(f: Filters, params: Pick<URLSearchParams, 'get'>): Filters {
  const site = params.get('source')
  return isSite(site) && site !== f.source ? { ...f, source: site } : f
}

/** The days of the freshness filter: a vacancy outside the cities of the questionnaire stays on the list so long after
 *  its publication on hh.ru. */
export const FRESH_DAYS = 5

export const DEFAULT_FILTERS: Filters = {
  group: 'candidates',
  min_score: '',
  search_id: '',
  selection_profile_id: '',
  area: '',
  work_format: '',
  experience: '',
  salary_from: '',
  q: '',
  include_snoozed: false,
  unseen_only: false,
  sort: 'score',
  fresh_days: String(FRESH_DAYS),
  max_responses: '',
  source: '',
}

export const FILTERS_KEY = 'vacancies.filters'
const SORTS = ['score', 'published', 'salary', 'responses']

/** How the page lays out the same list: `compact` (tabs, the filters in a popover, a dense list) or `classic` (every
 *  group as a button with its count, all the filters open in a panel, a table of ten columns). Kept per browser. */
export type VacancyLayout = 'compact' | 'classic'
export const LAYOUT_KEY = 'vacancies.layout'

export function loadLayout(storage: Pick<Storage, 'getItem'> = localStorage): VacancyLayout {
  try {
    return storage.getItem(LAYOUT_KEY) === 'classic' ? 'classic' : 'compact'
  } catch {
    return 'compact'
  }
}
const NUMERIC_FILTERS = ['min_score', 'salary_from', 'search_id', 'selection_profile_id']

export function isGroup(value: unknown): value is string {
  return typeof value === 'string' && GROUPS.some(([id]) => id === value)
}

export function savedFilterOk(key: string, value: unknown): boolean {
  if (typeof value !== typeof (DEFAULT_FILTERS as unknown as Record<string, unknown>)[key]) return false
  if (key === 'sort') return SORTS.includes(value as string)
  if (key === 'group') return isGroup(value)
  if (NUMERIC_FILTERS.includes(key)) return /^\d*$/.test(value as string)
  // 1–365 days or '' (off): «0» would be a 422 from the list
  if (key === 'fresh_days') return /^(?:|[1-9]\d{0,2})$/.test(value as string) && Number(value || 0) <= 365
  // 0–999 999 responses or '' (off), as the field types it (no leading zeros): the API takes no more
  if (key === 'max_responses') return /^(?:|0|[1-9]\d{0,5})$/.test(value as string)
  // a site the API knows or '' (every site): «hh.ru» or «HireHi» would be a 422
  if (key === 'source') return SITES.some(([id]) => id === value)
  return true
}

/** The standing filters of the list (freshness, responses cap) as a query: the list asks with them and so does the
 *  menu counter, so its number of new vacancies is the one the page shows. */
export function scopeQuery(f: Pick<Filters, 'fresh_days' | 'max_responses'>): string {
  const params = new URLSearchParams()
  if (f.fresh_days) params.set('fresh_days', f.fresh_days)
  if (f.max_responses) params.set('max_responses', f.max_responses)
  return params.toString()
}

/** `scopeQuery` of the saved filters (the menu reads them at every fetch). */
export function savedScopeQuery(storage: Pick<Storage, 'getItem'> = localStorage): string {
  return scopeQuery(loadFilters(storage))
}

/** The saved filters: only known keys with a value the API accepts — a stale or hand-edited entry ({"q": null},
 *  sort "date") must neither blank the tab nor turn the list into a 422. */
export function loadFilters(storage: Pick<Storage, 'getItem'> = localStorage): Filters {
  try {
    const raw = storage.getItem(FILTERS_KEY)
    const saved: unknown = raw ? JSON.parse(raw) : null
    if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
      const out: Record<string, unknown> = { ...DEFAULT_FILTERS }
      for (const [key, value] of Object.entries(saved)) {
        if (key in DEFAULT_FILTERS && savedFilterOk(key, value)) out[key] = value
      }
      return out as unknown as Filters
    }
  } catch {
    /* ignore */
  }
  return DEFAULT_FILTERS
}
