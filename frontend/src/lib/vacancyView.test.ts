import { describe, expect, it } from 'vitest'
import type { SavedSearch, SelectionProfile } from '../api/types'
import { DEFAULT_FILTERS } from './vacancyFilters'
import { clearFilters, filterChips, pipelineOf, rowFlags, rowStatus, salaryPublished } from './vacancyView'

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
  it('has no chip for the defaults: the tab, the sort and the text query are shown elsewhere', () => {
    expect(filterChips({ ...DEFAULT_FILTERS, group: 'sent', sort: 'salary', q: 'python' })).toEqual([])
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
    }
    const searches = [{ id: 7, name: 'QA-автоматизация' }] as SavedSearch[]
    expect(filterChips(f, searches).map((c) => sp(c.label))).toEqual([
      'Поиск: QA-автоматизация',
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
    const chips = filterChips({ ...DEFAULT_FILTERS, search_id: '41', selection_profile_id: '3' }, [], [] as SelectionProfile[])
    expect(chips.map((c) => c.label)).toEqual(['Поиск: №41', 'Критерии: №3'])
  })

  it('clears one experience level and keeps the others', () => {
    const f = { ...DEFAULT_FILTERS, experience: 'noExperience,between1And3' }
    const chip = filterChips(f).find((c) => c.key === 'exp-noExperience')!
    expect(chip.clear(f).experience).toBe('between1And3')
  })
})

describe('clearFilters', () => {
  it('keeps the tab and the sort order, drops everything else', () => {
    const f = { ...DEFAULT_FILTERS, group: 'maybe', sort: 'published', q: 'go', area: 'Москва', min_score: '50', unseen_only: true }
    expect(clearFilters(f)).toEqual({ ...DEFAULT_FILTERS, group: 'maybe', sort: 'published' })
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
