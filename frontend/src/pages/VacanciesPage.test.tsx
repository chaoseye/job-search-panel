import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { VacancyCounts, VacancyRow } from '../api/types'
import { FILTERS_KEY, LAYOUT_KEY } from '../lib/vacancyFilters'
import detailFixture from '../test/vacancy_detail.json'
import { renderAt, stubApi } from '../test/render'
import VacanciesPage from './VacanciesPage'

function counts(over: Partial<VacancyCounts> = {}): VacancyCounts {
  return {
    by_status: {},
    candidates: 0,
    suitable: 0,
    maybe: 0,
    prescored: 0,
    awaiting_claude: 0,
    needs_manual: 0,
    not_suitable: 0,
    filtered_out: 0,
    skipped: 0,
    hidden: 0,
    already_applied: 0,
    new: 0,
    snoozed: 0,
    unseen_candidates: 0,
    needs_clarification: 0,
    total: 0,
    ...over,
  }
}

const EMPTY_LIST = { items: [], total: 0, page: 0, per_page: 50, counts: counts() }
const FRESH = { days: 5, home_areas: ['Казань'], hidden: 0 }

function row(id: number, name: string): VacancyRow {
  return {
    id,
    hh_id: String(1000 + id),
    name,
    employer: { id: '1', name: 'Компания', accredited_it: false, trusted: null },
    area_name: 'Москва',
    address: null,
    metro: [],
    salary: { from: null, to: null, currency: null, gross: null, net_estimate: null, net_rur: null },
    experience_name: null,
    employment_form_name: null,
    work_formats: [],
    work_format_ids: [],
    published_at: null,
    first_seen_at: null,
    responses_count: null,
    duplicate_of_id: null,
    sources: [],
    flags: [],
    pipeline_status: 'suitable',
    status_label: 'подходит',
    pipeline_reason: null,
    prescore: 60,
    score: 80,
    verdict: 'apply',
    letter_status: 'ready',
    batch_id: null,
    selection_profile_id: null,
    selection_profile_name: null,
    alternate_url: null,
    apply_alternate_url: null,
    is_new: false,
    snoozed_until: null,
    hidden: false,
    user_override: false,
    archived: false,
    has_full: true,
  }
}

function listRequests(calls: string[]): URLSearchParams[] {
  return calls.filter((c) => c.startsWith('/api/vacancies?')).map((c) => new URLSearchParams(c.split('?')[1]))
}

describe('VacanciesPage', () => {
  it('opens the group named in the address (the dashboard block leads here)', async () => {
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': EMPTY_LIST })
    renderAt('/vacancies?group=needs_clarification', '/vacancies', <VacanciesPage />)
    await waitFor(() => expect(listRequests(api.calls).some((p) => p.get('group') === 'needs_clarification')).toBe(true))
    // the parameter is consumed: the address goes back to /vacancies, the choice is kept as a filter
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/vacancies$/))
    await waitFor(() => expect(JSON.parse(localStorage.getItem(FILTERS_KEY) ?? '{}').group).toBe('needs_clarification'))
  })

  it('ignores an unknown group in the address', async () => {
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': EMPTY_LIST })
    renderAt('/vacancies?group=drop_table', '/vacancies', <VacanciesPage />)
    await waitFor(() => expect(listRequests(api.calls).length).toBeGreaterThan(0))
    expect(listRequests(api.calls).every((p) => p.get('group') === 'candidates')).toBe(true)
  })

  it('does not trust a broken saved filter', async () => {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ q: null, sort: 'date', group: 'maybe' }))
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': EMPTY_LIST })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    await waitFor(() => expect(listRequests(api.calls).length).toBeGreaterThan(0))
    const first = listRequests(api.calls)[0]
    expect(first.get('group')).toBe('maybe')
    expect(first.get('sort')).toBe('score')
    expect(first.has('q')).toBe(false)
  })

  it('shows the saved filters as chips and explains an empty list', async () => {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ group: 'candidates', work_format: 'REMOTE', min_score: '60' }))
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': { ...EMPTY_LIST, counts: counts({ candidates: 12, total: 30 }) } })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('Формат: удалённо')).toBeInTheDocument()
    expect(screen.getByText('Оценка от 60')).toBeInTheDocument()
    expect(await screen.findByText('Ничего не найдено')).toBeInTheDocument()
    expect(screen.getByText(/В группе «Кандидаты» 12 вакансий, но поиск и фильтры скрывают все/)).toBeInTheDocument()

    // one cross takes away one filter, the others stay
    fireEvent.click(screen.getByRole('button', { name: 'Убрать фильтр «Формат: удалённо»' }))
    await waitFor(() => {
      const last = listRequests(api.calls).at(-1)!
      expect(last.has('work_format')).toBe(false)
      expect(last.get('min_score')).toBe('60')
    })
    expect(screen.queryByText('Формат: удалённо')).toBeNull()

    // «Сбросить все» clears the rest and keeps the tab
    fireEvent.click(screen.getByRole('button', { name: 'Сбросить все' }))
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.has('min_score')).toBe(false))
    expect(listRequests(api.calls).at(-1)!.get('group')).toBe('candidates')
    expect(screen.queryByText('Оценка от 60')).toBeNull()
    expect(JSON.parse(localStorage.getItem(FILTERS_KEY) ?? '{}').min_score).toBe('')
  })

  it('says how many candidates are new in the right word form', async () => {
    for (const [n, text] of [
      [21, '21 новый'],
      [2, '2 новых'],
      [11, '11 новых'],
    ] as const) {
      stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': { ...EMPTY_LIST, counts: counts({ candidates: 40, unseen_candidates: n, total: 40 }) } })
      const { unmount } = renderAt('/vacancies', '/vacancies', <VacanciesPage />)
      expect(await screen.findByText(`40 кандидатов · ${text}`)).toBeInTheDocument()
      unmount()
    }
  })

  it('hides the old vacancies of other cities by default and says how many', async () => {
    const api = stubApi({
      '/api/vacancies/hidden-employers': [],
      '/api/vacancies': { ...EMPTY_LIST, items: [row(5, 'Первая вакансия')], total: 1, counts: counts({ candidates: 1, total: 9 }), fresh: { ...FRESH, hidden: 69 } },
    })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    await waitFor(() => expect(listRequests(api.calls).length).toBeGreaterThan(0))
    expect(listRequests(api.calls)[0].get('fresh_days')).toBe('5')
    const chip = await screen.findByText('Другие города: до 5 дней')
    expect(chip.closest('[title]')).toHaveAttribute('title', 'Без срока: Казань (регионы анкеты) и вакансии с откликом в работе.')
    expect(await screen.findByText('1 вакансия · 69 старых скрыто')).toBeInTheDocument()
    // a default is nothing to reset
    expect(screen.queryByRole('button', { name: 'Сбросить все' })).toBeNull()

    // the cross turns it off and the browser keeps it off
    fireEvent.click(screen.getByRole('button', { name: 'Убрать фильтр «Другие города: до 5 дней»' }))
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.has('fresh_days')).toBe(false))
    expect(JSON.parse(localStorage.getItem(FILTERS_KEY) ?? '{}').fresh_days).toBe('')
    expect(screen.queryByText('Другие города: до 5 дней')).toBeNull()

    // the filters popover turns it back on and names the spared city
    fireEvent.click(screen.getByRole('button', { name: /^Фильтры/ }))
    const box = await screen.findByRole('checkbox', { name: 'Другие города: до 5 дней' })
    expect(box).not.toBeChecked()
    expect(screen.getByText('Без срока: Казань (регионы анкеты) и вакансии с откликом в работе.')).toBeInTheDocument()
    fireEvent.click(box)
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('fresh_days')).toBe('5'))
  })

  it('offers the old vacancies when the filter alone emptied the group', async () => {
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': { ...EMPTY_LIST, fresh: { ...FRESH, hidden: 3 } } })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('Свежих вакансий нет')).toBeInTheDocument()
    expect(screen.getByText('Вакансий из других городов старше 5 дней: 3. Фильтр их скрывает.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Показать старые' }))
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.has('fresh_days')).toBe(false))
  })

  it('caps the responses from the filters popover, waits for the whole number and says how many it hid', async () => {
    const api = stubApi({
      '/api/vacancies/hidden-employers': [],
      '/api/vacancies': { ...EMPTY_LIST, items: [row(5, 'Первая вакансия')], total: 1, counts: counts({ candidates: 1, total: 9 }), fresh: FRESH, responses: { max: 1000, hidden: 9 } },
    })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    await waitFor(() => expect(listRequests(api.calls).length).toBeGreaterThan(0))
    expect(listRequests(api.calls)[0].has('max_responses')).toBe(false) // off by default

    fireEvent.click(screen.getByRole('button', { name: /^Фильтры/ }))
    const field = await screen.findByLabelText('Откликов до')
    expect(screen.getByText('Пусто — без ограничения. Без счётчика откликов и с откликом в работе видны всегда.')).toBeInTheDocument()
    // typed key by key: the list waits for the whole number, «до 1» and «до 10» never go out
    for (const value of ['1', '10', '100', '1000']) fireEvent.change(field, { target: { value } })
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('max_responses')).toBe('1000'))
    expect(listRequests(api.calls).every((p) => [null, '1000'].includes(p.get('max_responses')))).toBe(true)
    expect(JSON.parse(localStorage.getItem(FILTERS_KEY) ?? '{}').max_responses).toBe('1000')
    // a sign or a fraction is not taken (the API would answer 422); a pasted «1 500» is the number 1500
    fireEvent.change(field, { target: { value: '-5' } })
    expect(field).toHaveValue('1000')
    fireEvent.change(field, { target: { value: '1 500' } })
    expect(field).toHaveValue('1500')
    expect(listRequests(api.calls).some((p) => p.get('max_responses') === '1500')).toBe(false) // the typing may go on
    // leaving the field (a click on «Готово» right away) sends the number without waiting for the pause
    fireEvent.blur(field)
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('max_responses')).toBe('1500'), { timeout: 200 })
    fireEvent.click(screen.getByRole('button', { name: 'Готово' }))

    const chip = await screen.findByText('Откликов до 1 500')
    expect(chip.closest('[title]')?.getAttribute('title')?.replace(/\s/g, ' ')).toBe(
      'Скрыты вакансии, где откликов на hh.ru больше 1 500. Без счётчика откликов и с откликом в работе видны всегда.',
    )
    expect(screen.getByText('1 вакансия · 9 скрыто по числу откликов')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Сбросить все' })).toBeInTheDocument() // not a default

    // the window closed by Escape right after the typing keeps the number too
    fireEvent.click(screen.getByRole('button', { name: /^Фильтры/ }))
    fireEvent.change(await screen.findByLabelText('Откликов до'), { target: { value: '700' } })
    fireEvent.keyDown(screen.getByRole('dialog', { name: 'Фильтры' }), { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Фильтры' })).toBeNull()
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('max_responses')).toBe('700'), { timeout: 200 })
    expect(await screen.findByText('Откликов до 700')).toBeInTheDocument()

    // the cross takes the cap away and the browser forgets it
    fireEvent.click(screen.getByRole('button', { name: 'Убрать фильтр «Откликов до 700»' }))
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.has('max_responses')).toBe(false))
    expect(JSON.parse(localStorage.getItem(FILTERS_KEY) ?? '{}').max_responses).toBe('')
    expect(screen.queryByText(/^Откликов до/)).toBeNull()
  })

  it('offers every vacancy when the responses cap alone emptied the group', async () => {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ max_responses: '1000' }))
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': { ...EMPTY_LIST, fresh: FRESH, responses: { max: 1000, hidden: 9 } } })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('Вакансий с откликами до 1 000 нет')).toBeInTheDocument()
    expect(screen.getByText('Вакансий, где откликов больше 1 000: 9. Фильтр их скрывает.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Показать старые' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Показать с любым числом откликов' }))
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.has('max_responses')).toBe(false))
    expect(listRequests(api.calls).at(-1)!.get('fresh_days')).toBe('5')
  })

  it('names both standing filters when both emptied the group and turns off only the one asked', async () => {
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ max_responses: '300' }))
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': { ...EMPTY_LIST, fresh: { ...FRESH, hidden: 3 }, responses: { max: 300, hidden: 2 } } })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('Свежих вакансий с откликами до 300 нет')).toBeInTheDocument()
    expect(screen.getByText('Вакансий из других городов старше 5 дней: 3; свежих, где откликов больше 300: 2. Фильтры их скрывают.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Показать с любым числом откликов' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Показать старые' }))
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.has('fresh_days')).toBe(false))
    expect(listRequests(api.calls).at(-1)!.get('max_responses')).toBe('300')
  })

  it('walks the list from the open card with J and K', async () => {
    const api = stubApi({
      '/api/vacancies/hidden-employers': [],
      '/api/vacancies/5': { ...detailFixture, id: 5, name: 'Первая вакансия', pipeline_status: 'suitable' },
      '/api/vacancies/6': { ...detailFixture, id: 6, name: 'Вторая вакансия', pipeline_status: 'suitable' },
      '/api/vacancies': { ...EMPTY_LIST, items: [row(5, 'Первая вакансия'), row(6, 'Вторая вакансия')], total: 2, counts: counts({ candidates: 2, total: 2 }) },
    })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Первая вакансия' }))
    expect(await screen.findByRole('heading', { name: 'Первая вакансия' })).toBeInTheDocument()
    expect(screen.getByText('1 из 2')).toBeInTheDocument()

    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'j', code: 'KeyJ' })
    expect(await screen.findByRole('heading', { name: 'Вторая вакансия' })).toBeInTheDocument()
    expect(api.calls).toContain('/api/vacancies/6')

    // K goes back; the layout does not matter (the physical key, not the letter: «л» on the Russian layout)
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'л', code: 'KeyK' })
    expect(await screen.findByRole('heading', { name: 'Первая вакансия' })).toBeInTheDocument()
  })
})

describe('VacanciesPage, classic view', () => {
  // the backend counts every group, also the ones the type does not list (queued, sent, watched)
  const list = (items: VacancyRow[], over: Record<string, number> = {}) => ({
    ...EMPTY_LIST,
    items,
    total: items.length,
    counts: { ...counts({ candidates: items.length, total: 40 }), sent: 0, queued: 0, watched: 0, ...over } as VacancyCounts,
  })

  it('switches to the structure from before the redesign and remembers the choice', async () => {
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': list([row(5, 'Первая вакансия')], { needs_manual: 2, sent: 7 }) })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    expect(await screen.findByRole('tablist', { name: 'Группы вакансий' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Классический' }))

    // every group at once with its count, the filters open, the old columns
    const groups = await screen.findByRole('radiogroup', { name: 'Группы вакансий' })
    expect(within(groups).getAllByRole('radio')).toHaveLength(16)
    expect(within(groups).getByRole('radio', { name: 'Кандидаты 1' })).toHaveAttribute('aria-checked', 'true')
    expect(within(groups).getByRole('radio', { name: 'Отправлено 7' })).toBeInTheDocument()
    expect(screen.queryByRole('tablist', { name: 'Группы вакансий' })).toBeNull()
    expect(screen.getByLabelText('Регион')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Фильтры/ })).toBeNull()
    for (const name of ['Оценка', 'Вакансия', 'Компания', 'Зарплата', 'Регион · формат', 'Опыт', 'Опубл.', 'Откл.', 'Статус']) {
      expect(screen.getByRole('columnheader', { name })).toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: 'Переоценить всё по критериям' })).toBeInTheDocument()
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('classic')

    // a group button asks the list for that group
    fireEvent.click(within(groups).getByRole('radio', { name: 'Нужно действие 2' }))
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('group')).toBe('needs_manual'))
    expect(JSON.parse(localStorage.getItem(FILTERS_KEY) ?? '{}').group).toBe('needs_manual')
  })

  it('filters from the open panel and resets them without leaving the group', async () => {
    localStorage.setItem(LAYOUT_KEY, 'classic')
    localStorage.setItem(FILTERS_KEY, JSON.stringify({ group: 'maybe' }))
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': list([]) })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    const reset = await screen.findByRole('button', { name: 'Сбросить фильтры' })
    expect(reset).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Формат работы'), { target: { value: 'REMOTE' } })
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('work_format')).toBe('REMOTE'))
    fireEvent.click(screen.getByRole('button', { name: '3–6 лет' }))
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('experience')).toBe('between3And6'))

    // the score takes 0–100 only: a keystroke past it is not taken (the API would answer 422)
    const score = screen.getByLabelText('Оценка от')
    fireEvent.change(score, { target: { value: '60' } })
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('min_score')).toBe('60'))
    fireEvent.change(score, { target: { value: '600' } })
    expect(score).toHaveValue(60)
    expect(listRequests(api.calls).some((p) => p.get('min_score') === '600')).toBe(false)

    expect(await screen.findByText('Ничего не найдено')).toBeInTheDocument()
    fireEvent.click(reset)
    await waitFor(() => {
      const last = listRequests(api.calls).at(-1)!
      expect(last.has('work_format')).toBe(false)
      expect(last.has('experience')).toBe(false)
      expect(last.has('min_score')).toBe(false)
      expect(last.get('group')).toBe('maybe')
    })
  })

  it('has the freshness filter among the open filters, on by default, and a reset turns it back on', async () => {
    localStorage.setItem(LAYOUT_KEY, 'classic')
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': { ...list([row(5, 'Первая вакансия')]), fresh: { ...FRESH, hidden: 4 } } })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    const box = await screen.findByRole('checkbox', { name: 'Другие города: до 5 дней' })
    expect(box).toBeChecked()
    expect(await screen.findByText('1 вакансия · 4 старые скрыты')).toBeInTheDocument()
    const reset = screen.getByRole('button', { name: 'Сбросить фильтры' })
    expect(reset).toBeDisabled()
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('fresh_days')).toBe('5'))

    fireEvent.click(box)
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.has('fresh_days')).toBe(false))
    expect(reset).toBeEnabled()
    fireEvent.click(reset)
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('fresh_days')).toBe('5'))
    expect(screen.getByRole('checkbox', { name: 'Другие города: до 5 дней' })).toBeChecked()
  })

  it('has the responses cap next to the freshness filter, and a reset clears it', async () => {
    localStorage.setItem(LAYOUT_KEY, 'classic')
    const api = stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': { ...list([row(5, 'Первая вакансия')]), fresh: FRESH, responses: { max: 500, hidden: 3 } } })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    const field = await screen.findByLabelText('Откликов до')
    expect(field).toHaveValue('')
    const reset = screen.getByRole('button', { name: 'Сбросить фильтры' })
    expect(reset).toBeDisabled()

    fireEvent.change(field, { target: { value: '500' } })
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.get('max_responses')).toBe('500'))
    expect(await screen.findByText('1 вакансия · 3 скрыты по числу откликов')).toBeInTheDocument()
    expect(reset).toBeEnabled()
    fireEvent.click(reset)
    await waitFor(() => expect(listRequests(api.calls).at(-1)!.has('max_responses')).toBe(false))
    expect(screen.getByLabelText('Откликов до')).toHaveValue('')
  })

  it('opens the card from a row of the table and walks it with J', async () => {
    localStorage.setItem(LAYOUT_KEY, 'classic')
    stubApi({
      '/api/vacancies/hidden-employers': [],
      '/api/vacancies/5': { ...detailFixture, id: 5, name: 'Первая вакансия', pipeline_status: 'suitable' },
      '/api/vacancies/6': { ...detailFixture, id: 6, name: 'Вторая вакансия', pipeline_status: 'suitable' },
      '/api/vacancies': list([row(5, 'Первая вакансия'), row(6, 'Вторая вакансия')]),
    })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    // the table is there while the list loads (grey rows), the vacancies come after
    const table = await screen.findByRole('table', { name: 'Вакансии' })
    fireEvent.click(await within(table).findByRole('button', { name: 'Первая вакансия' }))
    expect(await screen.findByRole('heading', { name: 'Первая вакансия' })).toBeInTheDocument()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'j', code: 'KeyJ' })
    expect(await screen.findByRole('heading', { name: 'Вторая вакансия' })).toBeInTheDocument()
  })

  it('selects rows and offers the response from the header', async () => {
    localStorage.setItem(LAYOUT_KEY, 'classic')
    stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': list([row(5, 'Первая вакансия'), row(6, 'Вторая вакансия')]) })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Выбрать все на странице' }))
    expect(screen.getByText('Выбрано 2')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Открыть по очереди на hh.ru (2)' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Снять выбор' }))
    expect(screen.queryByText('Выбрано 2')).toBeNull()
  })

  it('lists the hidden employers under the table and shows one again', async () => {
    localStorage.setItem(LAYOUT_KEY, 'classic')
    const api = stubApi({
      '/api/vacancies/hidden-employers': [{ id: 3, employer_id: '77', employer_name: 'Рога и копыта', created_at: null }],
      '/api/vacancies': list([]),
    })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    expect(await screen.findByRole('heading', { name: 'Скрытые работодатели' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Показывать снова: Рога и копыта' }))
    await waitFor(() => expect(api.requests.some((r) => r.method === 'DELETE' && r.path === '/api/vacancies/hidden-employers/3')).toBe(true))
  })

  it('keeps the compact view on a phone and the choice for a wider screen', async () => {
    localStorage.setItem(LAYOUT_KEY, 'classic')
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('max-width'), media: query, addEventListener: () => {}, removeEventListener: () => {} }))
    stubApi({ '/api/vacancies/hidden-employers': [], '/api/vacancies': list([]) })
    renderAt('/vacancies', '/vacancies', <VacanciesPage />)
    expect(await screen.findByRole('tablist', { name: 'Группы вакансий' })).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup', { name: 'Группы вакансий' })).toBeNull()
    expect(screen.queryByRole('radiogroup', { name: 'Вид страницы' })).toBeNull()
    expect(localStorage.getItem(LAYOUT_KEY)).toBe('classic')
  })
})

describe('VacancyDrawer actions', () => {
  // a detail answered by the real backend (backend vacancy_detail on a test vacancy), status set per case
  const detail = (status: string) => ({ ...detailFixture, pipeline_status: status })
  const routes = (status: string) => ({
    '/api/vacancies/hidden-employers': [],
    '/api/vacancies/5/questions': [],
    '/api/vacancies/5': detail(status),
    '/api/vacancies': EMPTY_LIST,
  })

  it('offers no return and no new packet when hh.ru already has a response', async () => {
    stubApi(routes('already_applied'))
    renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('На hh.ru уже есть отклик.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Вернуть в кандидаты' })).toBeNull()
    expect(screen.queryByRole('button', { name: /В пакет для Claude/ })).toBeNull()
    // the rarer actions live in the «…» menu: re-evaluation is there, a new packet is not
    fireEvent.click(screen.getByRole('button', { name: 'Ещё действия' }))
    expect(await screen.findByRole('menuitem', { name: /^Переоценить/ })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /Переразобрать в Claude/ })).toBeNull()
  })

  it('keeps the packet button out of a sent vacancy and in for a skipped one', async () => {
    stubApi(routes('sent'))
    const { unmount } = renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('Отклик отправлен.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /В пакет для Claude/ })).toBeNull()
    unmount()

    stubApi(routes('skipped'))
    renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    expect(await screen.findByRole('button', { name: 'Вернуть в кандидаты' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'В пакет для Claude' })).toBeInTheDocument()
  })

  // a letter of Claude and the answers to the employer's questions, as the backend gives them
  const LETTER = {
    id: 9,
    version: 1,
    text: 'Здравствуйте! Пишу по вакансии Python-разработчика.',
    language: 'ru',
    char_count: 50,
    origin: 'claude' as const,
    batch_id: null,
    mentions: [],
    needs_clarification: [],
    warnings: [],
    is_current: true,
    status: 'ready',
    created_at: '2026-11-07T10:00:00+00:00',
    send_problems: [],
  }
  const ANSWERS = {
    id: 4,
    vacancy_id: 5,
    questions: ['Какой у вас опыт с FastAPI?'],
    status: 'done' as const,
    status_label: 'готовы',
    batch_id: '20261107_1000_abcd',
    result: { answers: [{ question: 'Какой у вас опыт с FastAPI?', answer: 'Три года, платёжный сервис.', needs_clarification: [] }] },
    error: null,
    created_at: '2026-11-07T10:00:00+00:00',
    updated_at: null,
  }

  it('asks nothing of Claude and offers no response or return for a vacancy closed on hh.ru', async () => {
    const closed = { ...detail('filtered_out'), archived: true, prescore: 60, pipeline_reason: 'закрыта на hh.ru (архив)', letter: LETTER }
    stubApi({ ...routes('filtered_out'), '/api/vacancies/5': closed })
    const { unmount } = renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('Закрыта на hh.ru: откликнуться на неё нельзя. Если её откроют снова, добавьте её по ссылке на hh.ru.')).toBeInTheDocument()
    expect(screen.getByText('Причина: закрыта на hh.ru (архив).')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Оставить как кандидата' })).toBeNull()
    expect(screen.queryByRole('button', { name: /В пакет для Claude/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Открыть отклик на hh\.ru|Откликнуться через API/ })).toBeNull()
    // the letter stays readable, but Claude writes no new version and no answers to the form
    expect(screen.getByRole('textbox', { name: 'Текст письма' })).toHaveValue(LETTER.text)
    expect(screen.queryByRole('button', { name: 'Переделать в Claude…' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Вопросы работодателя при отклике/ })).toBeNull()
    unmount()

    // filtered out by the rules, still open on hh.ru: everything stays
    stubApi({ ...routes('filtered_out'), '/api/vacancies/5': { ...closed, archived: false, pipeline_reason: 'стоп-слово в названии' } })
    renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    expect(await screen.findByRole('button', { name: 'Оставить как кандидата' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'В пакет для Claude' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Открыть отклик на hh\.ru|Откликнуться через API/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Переделать в Claude…' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Вопросы работодателя при отклике/ })).toBeInTheDocument()
    expect(screen.queryByText(/^Закрыта на hh\.ru: откликнуться/)).toBeNull()
  })

  it('promises no letter from Claude for a closed vacancy without one', async () => {
    // without a letter and an evaluation the card opens on «Описание»: the letter tab is one click away
    stubApi({ ...routes('filtered_out'), '/api/vacancies/5': { ...detail('filtered_out'), archived: true, pipeline_reason: 'закрыта на hh.ru (архив)' } })
    const { unmount } = renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Письмо' }))
    expect(await screen.findByText('Письма нет: вакансия закрыта на hh.ru.')).toBeInTheDocument()
    expect(screen.queryByText(/Claude напишет его при разборе/)).toBeNull()
    unmount()

    stubApi({ ...routes('filtered_out'), '/api/vacancies/5': { ...detail('filtered_out'), pipeline_reason: 'стоп-слово в названии' } })
    renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Письмо' }))
    expect(await screen.findByText(/^Письма ещё нет: Claude напишет его при разборе/)).toBeInTheDocument()
  })

  it('keeps the answers of a closed vacancy readable and asks for no new ones', async () => {
    const closed = { ...detail('filtered_out'), archived: true, pipeline_reason: 'закрыта на hh.ru (архив)', letter: LETTER, question_answers: [ANSWERS] }
    stubApi({ ...routes('filtered_out'), '/api/vacancies/5': closed })
    renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('Три года, платёжный сервис.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Вопросы работодателя при отклике/ })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByRole('button', { name: 'Запросить ответы' })).toBeNull()
    expect(screen.queryByRole('textbox', { name: 'Вопросы работодателя' })).toBeNull()
  })

  it('shows the marks that change how to apply', async () => {
    stubApi({ ...routes('suitable'), '/api/vacancies/5': { ...detail('suitable'), flags: ['test_required', 'letter_required'] } })
    renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('тест обязателен')).toBeInTheDocument()
    expect(screen.getByText('письмо обязательно')).toBeInTheDocument()
  })
})
