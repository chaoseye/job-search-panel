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

  it('shows the marks that change how to apply', async () => {
    stubApi({ ...routes('suitable'), '/api/vacancies/5': { ...detail('suitable'), flags: ['test_required', 'letter_required'] } })
    renderAt('/vacancies?open=5', '/vacancies', <VacanciesPage />)
    expect(await screen.findByText('тест обязателен')).toBeInTheDocument()
    expect(screen.getByText('письмо обязательно')).toBeInTheDocument()
  })
})
