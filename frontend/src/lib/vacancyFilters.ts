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
}

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
  return true
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
