import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { HireHiOptions, HireHiPreview, JobsView, SavedSearch, SearchOptions, SearchParams } from '../api/types'
import { renderAt, stubApi } from '../test/render'
import SearchesPage from './SearchesPage'

const PARAMS: SearchParams = {
  text: '',
  search_field: [],
  experience: [],
  employment_form: [],
  work_format: [],
  work_schedule_by_days: [],
  salary: null,
  currency: 'RUR',
  only_with_salary: false,
  label: [],
  professional_role: [],
  industry: [],
  areas: [],
  excluded_text: '',
  education: [],
  period_days: null,
}

const HH_OPTIONS: SearchOptions = {
  experience: [],
  employment_form: [],
  work_format: [],
  work_schedule_by_days: [],
  vacancy_label: [],
  search_field: [],
  currency: [{ id: 'RUR', name: 'Рубли' }],
  education: [],
  industry: [],
  similar_available: true,
  resume_id: null,
}

// a part of the catalogue of the site, as GET /api/searches/hirehi/options gives it
const HIREHI_OPTIONS: HireHiOptions = {
  categories: [
    { id: 'development', name: 'Разработка' },
    { id: 'backend', name: 'Backend' },
  ],
  subcategories: [{ id: 'python', name: 'Python' }],
  cities: [{ id: 'kazan', name: 'Казань' }],
  formats: [
    { id: 'remote_russia', name: 'Удалённо по РФ' },
    { id: 'гибрид', name: 'Гибрид' },
  ],
  levels: [
    { id: 'middle', name: 'Middle' },
    { id: 'senior', name: 'Senior' },
  ],
  min_interval: 60,
  state: { blocked_until: null, reason: null, checked_at: null },
}

const PREVIEW: HireHiPreview = {
  found: 412,
  page: 1,
  has_more: true,
  url: 'https://hirehi.ru/vacancies/backend?format=remote_russia',
  matched: 1,
  items: [
    {
      id: 'a1',
      name: 'Backend-инженер',
      employer_name: 'Рога и копыта',
      format: 'Удалённо по РФ',
      level: 'senior',
      category: 'backend',
      salary: 'от 300 000 ₽',
      published_at: null,
      alternate_url: 'https://hirehi.ru/vacancy/backend-engineer-a1',
      reason: null,
    },
    {
      id: 'b2',
      name: 'Аналитик данных',
      employer_name: 'Ромашка',
      format: 'Удалённо по РФ',
      level: 'middle',
      category: 'backend',
      salary: null,
      published_at: null,
      alternate_url: 'https://hirehi.ru/vacancy/data-analyst-b2',
      reason: 'в названии нет слов поиска',
    },
  ],
}

const JOBS: JobsView = { running: [], runs: [], last_scan_at: null, last_sync_at: null, paused_reason: null, failures: {} }

const FILTERS = { section: 'backend', city: '', formats: ['remote_russia'], levels: [], keywords: ['Python'] }

function search(over: Partial<SavedSearch> = {}): SavedSearch {
  return {
    id: 3,
    name: 'Python удалённо',
    source: 'hirehi',
    params: PARAMS,
    hirehi: FILTERS,
    summary: 'Backend · удалённо по РФ · слова: Python',
    interval_min: 60,
    enabled: true,
    watch_only: false,
    selection_profile_id: null,
    selection_profile_name: null,
    last_run_at: null,
    last_seen_published_at: null,
    last_found: null,
    last_new: null,
    last_requests: null,
    last_error: null,
    last_warning: null,
    created_at: null,
    ...over,
  }
}

function routes(searches: SavedSearch[] = [], hirehi: HireHiOptions = HIREHI_OPTIONS, jobs: JobsView = JOBS) {
  return {
    '/api/searches/options': HH_OPTIONS,
    '/api/searches/hirehi/options': hirehi,
    '/api/searches/preview': PREVIEW,
    '/api/searches': searches,
    '/api/selection-profiles': { items: [] },
    '/api/jobs': jobs,
  }
}

describe('SearchesPage, HireHi', () => {
  it('creates a HireHi search from the filters of the site and the words of the panel', async () => {
    const api = stubApi(routes())
    renderAt('/searches', '/searches', <SearchesPage />)
    fireEvent.click((await screen.findAllByRole('button', { name: 'Новый поиск' }))[0])
    const sheet = await screen.findByRole('dialog', { name: 'Новый поиск' })
    fireEvent.change(within(sheet).getByLabelText('Название'), { target: { value: 'Python удалённо' } })
    fireEvent.change(within(sheet).getByLabelText('Источник'), { target: { value: 'hirehi' } })

    // HireHi is read at most once an hour; the query of hh.ru gives way to the filters of the site
    expect(within(sheet).getByLabelText(/^Интервал, мин/)).toHaveValue(60)
    expect(within(sheet).getByLabelText(/^Интервал, мин/)).toHaveAttribute('min', '60')
    expect(within(sheet).queryByText('Текст запроса')).toBeNull()

    // without a city the subcategories are there; a city page knows the categories only
    const section = within(sheet).getByLabelText('Раздел')
    expect(await within(section).findByRole('option', { name: 'Python' })).toBeInTheDocument()
    fireEvent.change(section, { target: { value: 'python' } })
    fireEvent.change(within(sheet).getByLabelText(/^Город/), { target: { value: 'kazan' } })
    expect(section).toHaveValue('')
    expect(within(section).queryByRole('option', { name: 'Python' })).toBeNull()
    fireEvent.change(within(sheet).getByLabelText(/^Город/), { target: { value: '' } })
    fireEvent.change(section, { target: { value: 'backend' } })

    fireEvent.click(within(sheet).getByRole('button', { name: 'Удалённо по РФ' }))
    const words = within(sheet).getByLabelText('Слова в названии')
    fireEvent.change(words, { target: { value: 'Python' } })
    fireEvent.keyDown(words, { key: 'Enter' })

    // «Проверить» reads one listing page and says what the panel's filters do with each vacancy
    fireEvent.click(within(sheet).getByRole('button', { name: 'Проверить' }))
    const found = await within(sheet).findByRole('region', { name: 'Что найдёт поиск' })
    expect(within(found).getByText(/^На HireHi: 412/)).toBeInTheDocument()
    expect(within(found).getByText(/поиск возьмёт 1/)).toBeInTheDocument()
    expect(within(found).getByText('— не возьмёт: в названии нет слов поиска')).toBeInTheDocument()
    // the level as the form names it, not as the listing writes it
    expect(within(found).getByText(/· Senior · от 300 000 ₽/)).toBeInTheDocument()
    expect(api.requests.find((r) => r.path === '/api/searches/preview')?.body).toEqual({ source: 'hirehi', hirehi: FILTERS })

    fireEvent.click(within(sheet).getByRole('button', { name: 'Сохранить' }))
    await waitFor(() => expect(api.requests.some((r) => r.method === 'POST' && r.path === '/api/searches')).toBe(true))
    expect(api.requests.find((r) => r.method === 'POST' && r.path === '/api/searches')?.body).toMatchObject({
      name: 'Python удалённо',
      source: 'hirehi',
      interval_min: 60,
      hirehi: FILTERS,
    })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Новый поиск' })).toBeNull())
  })

  it('lists a HireHi search under its site, keeps its filters when turned off and reads it apart from the scan', async () => {
    const run = {
      id: 9,
      job: 'hirehi_sync',
      status: 'ok' as const,
      started_at: '2026-11-08T09:00:00+00:00',
      finished_at: '2026-11-08T09:00:40+00:00',
      duration_sec: 40,
      stats: { searches: [{ new: 3 }], processed: 3, by_status: { prescored: 2, skipped: 1 }, via_hh: 1, cards: 2 },
      error: null,
    }
    const api = stubApi(routes([search()], HIREHI_OPTIONS, { ...JOBS, runs: [run] }))
    renderAt('/searches', '/searches', <SearchesPage />)
    const table = await screen.findByRole('table', { name: 'Поиски' })
    expect(within(table).getByText('HireHi')).toBeInTheDocument()
    expect(within(table).getByText('Backend · удалённо по РФ · слова: Python')).toBeInTheDocument()
    expect(await screen.findByText('HireHi: поисков 1, новых 3 · обработано 3: кандидатов 2, через hh.ru 1, карточек 2')).toBeInTheDocument()

    fireEvent.click(within(table).getByRole('button', { name: 'Действия с поиском «Python удалённо»' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Выключить' }))
    await waitFor(() => expect(api.requests.some((r) => r.method === 'PUT' && r.path === '/api/searches/3')).toBe(true))
    expect(api.requests.find((r) => r.method === 'PUT')?.body).toMatchObject({ source: 'hirehi', enabled: false, interval_min: 60, hirehi: FILTERS })

    fireEvent.click(screen.getByRole('button', { name: 'Прочитать HireHi' }))
    await waitFor(() => expect(api.requests.some((r) => r.method === 'POST' && r.path === '/api/jobs/hirehi')).toBe(true))
    expect(api.requests.some((r) => r.path === '/api/jobs/scan')).toBe(false)
  })

  it('opens a saved HireHi search with its filters', async () => {
    stubApi(routes([search()]))
    renderAt('/searches', '/searches', <SearchesPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Python удалённо' }))
    const sheet = await screen.findByRole('dialog', { name: 'Изменить поиск' })
    expect(within(sheet).getByLabelText('Источник')).toHaveValue('hirehi')
    await waitFor(() => expect(within(sheet).getByLabelText('Раздел')).toHaveValue('backend'))
    expect(within(sheet).getByRole('button', { name: 'Удалённо по РФ' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(sheet).getByRole('button', { name: 'Удалить Python' })).toBeInTheDocument()
  })

  it('says HireHi refused the panel and keeps its reading off until then', async () => {
    const until = new Date(Date.now() + 5 * 3600_000).toISOString()
    stubApi(routes([search()], { ...HIREHI_OPTIONS, state: { blocked_until: until, reason: 'HireHi ответил 429 (слишком много запросов)', checked_at: null } }))
    renderAt('/searches', '/searches', <SearchesPage />)
    expect(await screen.findByText('HireHi закрыл доступ панели')).toBeInTheDocument()
    expect(screen.getByText(/^HireHi ответил 429 \(слишком много запросов\)\. Панель не читает HireHi до .+ и ничего не обходит/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Прочитать HireHi' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Запустить' })).toBeDisabled()
  })

  it('keeps the HireHi button away while there is no HireHi search', async () => {
    stubApi(routes([search({ id: 4, name: 'Python', source: 'search', hirehi: null, summary: 'python', interval_min: 30 })]))
    renderAt('/searches', '/searches', <SearchesPage />)
    const table = await screen.findByRole('table', { name: 'Поиски' })
    expect(within(table).getByText('hh.ru')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Прочитать HireHi' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Сканировать всё' })).toBeEnabled()
  })
})
