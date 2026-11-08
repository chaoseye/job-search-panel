import { describe, expect, it } from 'vitest'
import type { SavedSearch, SelectionProfile } from '../api/types'
import { DEFAULT_FILTERS } from './vacancyFilters'
import {
  canResetFilters,
  cappedText,
  clearFilters,
  filterChips,
  freshHint,
  freshLabel,
  hirehiApplyHint,
  isHireHi,
  pipelineOf,
  responsesHint,
  responsesLabel,
  rowFlags,
  rowStatus,
  salaryPublished,
  scopeEmpty,
  SITE_HINT,
  siteOf,
  staleText,
  unavailableHint,
} from './vacancyView'

const sp = (s: string | null) => s?.replace(/\s/g, ' ') ?? null // ru-RU groups thousands with a (narrow) no-break space

const salary = (over: Partial<{ from: number | null; to: number | null; currency: string | null; gross: boolean | null }>) => ({
  from: null,
  to: null,
  currency: 'RUR',
  gross: false,
  net_estimate: null,
  net_rur: null,
  ...over,
})

describe('filterChips', () => {
  it('has only the freshness chip for the defaults: the tab, the sort and the text query are shown elsewhere', () => {
    const chips = filterChips({ ...DEFAULT_FILTERS, group: 'sent', sort: 'salary', q: 'python' }, [], [], ['Казань'])
    expect(chips.map((c) => [c.key, c.label, c.title])).toEqual([
      ['fresh', 'Другие города: до 5 дней', 'Без срока: Казань (регионы анкеты) и вакансии с откликом в работе.'],
    ])
    expect(filterChips({ ...DEFAULT_FILTERS, fresh_days: '' })).toEqual([])
  })

  it('turns the freshness filter off with its cross', () => {
    const chip = filterChips(DEFAULT_FILTERS).find((c) => c.key === 'fresh')!
    expect(chip.clear(DEFAULT_FILTERS)).toEqual({ ...DEFAULT_FILTERS, fresh_days: '' })
  })

  it('shows the responses cap next to the freshness filter and clears it with its cross', () => {
    const f = { ...DEFAULT_FILTERS, max_responses: '1000' }
    const chips = filterChips(f, [], [], ['Казань'])
    expect(chips.map((c) => [c.key, sp(c.label), sp(c.title ?? null)])).toEqual([
      ['fresh', 'Другие города: до 5 дней', 'Без срока: Казань (регионы анкеты) и вакансии с откликом в работе.'],
      ['responses', 'Откликов до 1 000', 'Скрыты вакансии, где откликов на hh.ru больше 1 000. Без счётчика откликов и с откликом в работе видны всегда.'],
    ])
    expect(chips[1].clear(f)).toEqual(DEFAULT_FILTERS)
    // 0 is a cap too: only the vacancies nobody answered yet
    expect(filterChips({ ...DEFAULT_FILTERS, fresh_days: '', max_responses: '0' }).map((c) => c.label)).toEqual(['Откликов до 0'])
  })

  it('names every filter that hides part of the list', () => {
    const f = {
      ...DEFAULT_FILTERS,
      search_id: '7',
      selection_profile_id: '0',
      work_format: 'HYBRID',
      experience: 'between1And3,moreThan6',
      area: ' Казань ',
      salary_from: '150000',
      min_score: '70',
      unseen_only: true,
      include_snoozed: true,
      fresh_days: '7',
      max_responses: '300',
      source: 'hirehi',
    }
    const searches = [{ id: 7, name: 'QA-автоматизация' }] as SavedSearch[]
    expect(filterChips(f, searches, [], []).map((c) => sp(c.label))).toEqual([
      'Не старше 7 дней',
      'Откликов до 300',
      'Поиск: QA-автоматизация',
      'Сайт: HireHi',
      'Критерии: основная анкета',
      'Формат: гибрид',
      'Опыт: 1–3 года',
      'Опыт: 6+ лет',
      'Регион: Казань',
      'На руки от 150 000 ₽',
      'Оценка от 70',
      'Только новые',
      'С отложенными',
    ])
  })

  it('falls back to the number of a search or a set that is gone', () => {
    const chips = filterChips({ ...DEFAULT_FILTERS, search_id: '41', selection_profile_id: '3', fresh_days: '' }, [], [] as SelectionProfile[])
    expect(chips.map((c) => c.label)).toEqual(['Поиск: №41', 'Критерии: №3'])
  })

  it('clears one experience level and keeps the others', () => {
    const f = { ...DEFAULT_FILTERS, experience: 'noExperience,between1And3' }
    const chip = filterChips(f).find((c) => c.key === 'exp-noExperience')!
    expect(chip.clear(f).experience).toBe('between1And3')
  })

  it('names the site, says what «hh.ru» takes and clears it with its cross', () => {
    const f = { ...DEFAULT_FILTERS, fresh_days: '', source: 'hh' }
    const [chip] = filterChips(f)
    expect([chip.key, chip.label, chip.title]).toEqual(['site', 'Сайт: hh.ru', SITE_HINT])
    expect(SITE_HINT).toBe('Вакансия, которую HireHi взял с hh.ru, считается вакансией hh.ru.')
    expect(chip.clear(f)).toEqual({ ...DEFAULT_FILTERS, fresh_days: '' })
    expect(filterChips({ ...f, source: 'hirehi' }).map((c) => c.label)).toEqual(['Сайт: HireHi'])
  })
})

describe('clearFilters', () => {
  it('keeps the tab and the sort order, drops everything else', () => {
    const f = { ...DEFAULT_FILTERS, group: 'maybe', sort: 'published', q: 'go', area: 'Москва', min_score: '50', unseen_only: true }
    expect(clearFilters(f)).toEqual({ ...DEFAULT_FILTERS, group: 'maybe', sort: 'published' })
  })

  it('turns the freshness filter back on and clears the responses cap and the site: the defaults', () => {
    expect(clearFilters({ ...DEFAULT_FILTERS, fresh_days: '' }).fresh_days).toBe('5')
    expect(clearFilters({ ...DEFAULT_FILTERS, max_responses: '1000' }).max_responses).toBe('')
    expect(clearFilters({ ...DEFAULT_FILTERS, source: 'hirehi' }).source).toBe('')
  })
})

describe('canResetFilters', () => {
  it('has nothing to reset in the defaults, whatever the tab and the sort', () => {
    expect(canResetFilters(DEFAULT_FILTERS)).toBe(false)
    expect(canResetFilters({ ...DEFAULT_FILTERS, group: 'sent', sort: 'salary' })).toBe(false)
  })
  it('resets a filter turned off or set', () => {
    expect(canResetFilters({ ...DEFAULT_FILTERS, fresh_days: '' })).toBe(true)
    expect(canResetFilters({ ...DEFAULT_FILTERS, max_responses: '1000' })).toBe(true)
    expect(canResetFilters({ ...DEFAULT_FILTERS, source: 'hh' })).toBe(true)
    expect(canResetFilters({ ...DEFAULT_FILTERS, q: 'go' })).toBe(true)
    expect(canResetFilters({ ...DEFAULT_FILTERS, include_snoozed: true })).toBe(true)
  })
})

describe('the freshness filter in words', () => {
  it('says «другие города» while the questionnaire names cities, every city otherwise', () => {
    expect(freshLabel(5, ['Казань'])).toBe('Другие города: до 5 дней')
    expect(freshLabel(1, ['Казань'])).toBe('Другие города: до 1 дня')
    expect(freshLabel(3, [])).toBe('Не старше 3 дней')
    expect(freshLabel(5)).toBe('Другие города: до 5 дней') // not loaded yet
  })
  it('names the spared cities in the hint', () => {
    expect(freshHint(['Казань', 'Пермь'])).toBe('Без срока: Казань, Пермь (регионы анкеты) и вакансии с откликом в работе.')
    expect(freshHint([])).toBe('Без срока только вакансии с откликом в работе: в анкете не указаны регионы.')
    expect(freshHint()).toBe('Без срока: города из регионов анкеты и вакансии с откликом в работе.')
  })
  it('counts what it hid with the right word form', () => {
    expect(staleText(1)).toBe('1 старая скрыта')
    expect(staleText(3)).toBe('3 старые скрыты')
    expect(staleText(69)).toBe('69 старых скрыто')
    expect(staleText(21)).toBe('21 старая скрыта')
    expect(sp(staleText(1500))).toBe('1 500 старых скрыто')
  })
})

describe('the responses cap in words', () => {
  it('names the cap and what it spares', () => {
    expect(sp(responsesLabel(1000))).toBe('Откликов до 1 000')
    expect(responsesLabel(50)).toBe('Откликов до 50')
    expect(responsesHint(null)).toBe('Пусто — без ограничения. Без счётчика откликов и с откликом в работе видны всегда.')
  })
  it('counts what it hid with the right word form', () => {
    expect(cappedText(1)).toBe('1 скрыта по числу откликов')
    expect(cappedText(3)).toBe('3 скрыты по числу откликов')
    expect(cappedText(9)).toBe('9 скрыто по числу откликов')
    expect(sp(cappedText(1200))).toBe('1 200 скрыто по числу откликов')
  })
})

describe('scopeEmpty: the list the standing filters emptied', () => {
  const base = { stale: 0, capped: 0, days: 5, cap: null, homeAreas: ['Казань'] }
  it('names the old vacancies when only freshness hid them', () => {
    expect(scopeEmpty({ ...base, stale: 69 })).toEqual({ title: 'Свежих вакансий нет', text: 'Вакансий из других городов старше 5 дней: 69. Фильтр их скрывает.' })
    expect(scopeEmpty({ ...base, stale: 2, days: 1, homeAreas: [] }).text).toBe('Вакансий старше 1 дня: 2. Фильтр их скрывает.')
  })
  it('names the crowded vacancies when only the cap hid them', () => {
    const e = scopeEmpty({ ...base, capped: 9, cap: 1000 })
    expect([sp(e.title), sp(e.text)]).toEqual(['Вакансий с откликами до 1 000 нет', 'Вакансий, где откликов больше 1 000: 9. Фильтр их скрывает.'])
  })
  it('names both when both hid some: the cap counts only the fresh ones', () => {
    const e = scopeEmpty({ ...base, stale: 3, capped: 2, cap: 300 })
    expect([e.title, e.text]).toEqual(['Свежих вакансий с откликами до 300 нет', 'Вакансий из других городов старше 5 дней: 3; свежих, где откликов больше 300: 2. Фильтры их скрывают.'])
  })
})

describe('salaryPublished', () => {
  it('writes a range in thousands', () => {
    expect(salaryPublished(salary({ from: 120000, to: 200000 }))).toBe('120–200 тыс. ₽')
  })
  it('marks an amount before tax', () => {
    expect(salaryPublished(salary({ from: 80000, gross: true }))).toBe('от 80 тыс. ₽ до вычета')
  })
  it('keeps small amounts as they are', () => {
    expect(sp(salaryPublished(salary({ to: 2000, currency: 'USD' })))).toBe('до 2 000 $')
  })
  it('is empty without a salary', () => {
    expect(salaryPublished(salary({}))).toBeNull()
  })
})

describe('rowFlags', () => {
  it('keeps the marks that change how to apply, the strongest test mark only', () => {
    expect(rowFlags(['test', 'test_required', 'no_salary', 'letter_required', 'direct']).map((f) => f.label)).toEqual([
      'тест обязателен',
      'письмо обязательно',
      'внешний отклик',
    ])
  })
})

describe('rowStatus', () => {
  it('shows the stage of a vacancy that is no longer a candidate', () => {
    expect(rowStatus({ pipeline_status: 'skipped', letter_status: 'ready', status_label: 'пропущена' })).toEqual({ label: 'Пропущена', tone: 'neutral' })
  })
  it('shows the letter of a candidate: the next step', () => {
    expect(rowStatus({ pipeline_status: 'suitable', letter_status: 'needs_clarification', status_label: 'подходит' })).toEqual({ label: 'Уточнить письмо', tone: 'warn' })
    expect(rowStatus({ pipeline_status: 'letter_ready', letter_status: 'edited', status_label: 'письмо готово' })).toEqual({ label: 'Письмо готово', tone: 'good' })
  })
})

describe('pipelineOf', () => {
  const states = (p: ReturnType<typeof pipelineOf>) => p.steps.map((s) => `${s.label}:${s.state}`)

  it('puts a candidate with a ready letter on the response step', () => {
    expect(states(pipelineOf({ pipeline_status: 'letter_ready', score: 82, letter_status: 'ready' }))).toEqual([
      'Найдена:done',
      'Отбор:done',
      'Оценка Claude:done',
      'Письмо:done',
      'Отклик:current',
    ])
  })

  it('stops on the letter when it needs the user', () => {
    const p = pipelineOf({ pipeline_status: 'suitable', score: 75, letter_status: 'needs_clarification' })
    expect(p.steps.find((s) => s.state === 'attention')?.label).toBe('Письмо')
  })

  it('ends the track where a vacancy left the way', () => {
    const filtered = pipelineOf({ pipeline_status: 'filtered_out', score: null, letter_status: 'none' })
    expect(states(filtered)).toEqual(['Найдена:done'])
    expect(filtered.exit?.label).toBe('Отсеяна правилами')
    expect(pipelineOf({ pipeline_status: 'not_suitable', score: 30, letter_status: 'none' }).exit?.label).toBe('Claude: не подходит')
    expect(pipelineOf({ pipeline_status: 'not_suitable', score: null, letter_status: 'none' }).exit?.label).toBe('Ниже порога предоценки')
    expect(pipelineOf({ pipeline_status: 'skipped', score: 71, letter_status: 'ready' }).steps).toHaveLength(3)
  })

  it('marks every step done once the response is out', () => {
    expect(pipelineOf({ pipeline_status: 'sent', score: 80, letter_status: 'ready' }).steps.every((s) => s.state === 'done')).toBe(true)
  })
})

describe('the site of a vacancy', () => {
  it('is hh.ru for a row without a source and HireHi for the rows of HireHi', () => {
    expect(siteOf({})).toBe('hh.ru')
    expect(siteOf(null)).toBe('hh.ru')
    expect(siteOf({ source: 'hh' })).toBe('hh.ru')
    expect(siteOf({ source: 'hirehi' })).toBe('HireHi')
    expect(siteOf({ source: 'hirehi', source_label: 'HireHi' })).toBe('HireHi')
    expect(isHireHi({ source: 'hirehi' })).toBe(true)
    expect(isHireHi({ source: 'hh' })).toBe(false)
    expect(isHireHi(undefined)).toBe(false)
  })

  it('names the site in the hints', () => {
    expect(unavailableHint('HireHi')).toBe('HireHi пишет, что вакансия недоступна: отклик отменится, вакансия уйдёт в «Скрытые»')
    expect(unavailableHint('hh.ru')).toMatch(/^hh\.ru пишет/)
    // the button of the card is named as the site names it, «Отклик» when the card did not say
    expect(hirehiApplyHint('Откликнуться')).toContain('кнопка «Откликнуться»')
    expect(hirehiApplyHint(null)).toContain('кнопка «Отклик»')
    expect(hirehiApplyHint(null)).toContain('Нужен вход на HireHi')
  })
})
