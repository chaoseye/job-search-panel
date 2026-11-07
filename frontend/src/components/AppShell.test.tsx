import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { goToLogin } from '../api/login'
import VacanciesPage from '../pages/VacanciesPage'
import { stubApi, Where } from '../test/render'
import AppShell from './AppShell'

vi.mock('../api/login', () => ({ goToLogin: vi.fn() }))

function renderShell(url: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="*" element={<Where />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const API = {
  '/api/vacancies/summary': { unseen_candidates: 12 },
  '/api/negotiations/status': { with_updates: 2 },
  '/api/batches/summary': { pending_batches: 0 },
  '/api/auth/status': { logged_in: true, state: 'expired', expires_at: null, user: null },
  '/api/settings': { apply_mode: 'assist', paused_reason: 'user', captcha_url: null },
  '/api/applications/status': { limits: { today_sent: 3, max_per_day: 20 } },
}

// the phone header: the menu button and the name of the open section
const phoneHeader = () => screen.getByRole('button', { name: 'Открыть меню разделов' }).parentElement!

describe('AppShell', () => {
  it('groups the sections, marks the open one and counts the queues that wait', async () => {
    stubApi(API)
    renderShell('/vacancies?group=new')
    const nav = screen.getByRole('navigation', { name: 'Разделы' })
    expect(within(nav).getAllByRole('listitem')).toHaveLength(14)
    expect(await within(nav).findByRole('link', { name: 'Вакансии 12' })).toHaveAttribute('aria-current', 'page')
    // each count comes with its own request
    expect(await within(nav).findByRole('link', { name: 'Отклики 2' })).not.toHaveAttribute('aria-current')
    // an empty queue shows no count
    expect(within(nav).getByRole('link', { name: 'Разбор' })).toBeInTheDocument()
    for (const name of ['Сводка', 'Воронка', 'Профиль и критерии', 'Настройки']) expect(within(nav).getByRole('link', { name })).toBeInTheDocument()
    expect(within(phoneHeader()).getByText('Вакансии')).toBeInTheDocument()

    expect(await screen.findByText('токен истёк, войдите заново')).toBeInTheDocument()
    expect(await screen.findByText('Режим «Ассистент»')).toBeInTheDocument()
    expect(await screen.findByText(/Откликов сегодня:/)).toHaveTextContent('Откликов сегодня: 3 из 20')
    expect(await screen.findByText(/Пауза включена вручную\./)).toBeInTheDocument()
  })

  it('counts the new vacancies with the standing filters the «Вакансии» page keeps', async () => {
    const api = stubApi(API)
    const { unmount } = renderShell('/')
    await waitFor(() => expect(api.calls).toContain('/api/vacancies/summary?fresh_days=5')) // on by default
    unmount()

    localStorage.setItem('vacancies.filters', JSON.stringify({ fresh_days: '' })) // turned off on the page
    const off = stubApi(API)
    const second = renderShell('/')
    await waitFor(() => expect(off.calls).toContain('/api/vacancies/summary'))
    expect(off.calls.some((c) => c.includes('fresh_days'))).toBe(false)
    second.unmount()

    localStorage.setItem('vacancies.filters', JSON.stringify({ max_responses: '1000' })) // the responses cap too
    const capped = stubApi(API)
    renderShell('/')
    await waitFor(() => expect(capped.calls).toContain('/api/vacancies/summary?fresh_days=5&max_responses=1000'))
  })

  it('refreshes that count when the «Вакансии» page sets a standing filter', async () => {
    const api = stubApi({
      ...API,
      '/api/vacancies/hidden-employers': [],
      '/api/vacancies': { items: [], total: 0, page: 0, per_page: 50, counts: { candidates: 0, total: 0, unseen_candidates: 0, new: 0, snoozed: 0 } },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/vacancies']}>
          <Routes>
            <Route element={<AppShell />}>
              <Route path="/vacancies" element={<VacanciesPage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(api.calls).toContain('/api/vacancies/summary?fresh_days=5'))
    fireEvent.click(await screen.findByRole('button', { name: /^Фильтры/ }))
    fireEvent.change(await screen.findByLabelText('Откликов до'), { target: { value: '1000' } })
    await waitFor(() => expect(api.calls).toContain('/api/vacancies/summary?fresh_days=5&max_responses=1000'))
  })

  it('opens the menu on a phone and closes it on a tap, also on the page already open', async () => {
    stubApi(API)
    renderShell('/searches')
    fireEvent.click(screen.getByRole('button', { name: 'Открыть меню разделов' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Job Search Panel' })).getByRole('link', { name: 'Поиски' }))
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Открыть меню разделов' }))
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Job Search Panel' })).getByRole('link', { name: 'Аналитика' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/analytics$/))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(within(phoneHeader()).getByText('Аналитика')).toBeInTheDocument()
  })
})

describe('AppShell through the public address', () => {
  it('shows until when the link session lasts and signs this browser out', async () => {
    const { requests } = stubApi({ ...API, '/api/session': { via: 'link', created_at: '2026-10-23T08:00:00Z', expires_at: '2026-10-23T20:00:00Z' } })
    renderShell('/vacancies')
    const nav = screen.getByRole('navigation', { name: 'Разделы' }).parentElement!
    expect(await within(nav).findByText(/^Вход по ссылке до /)).toBeInTheDocument()
    fireEvent.click(within(nav).getByRole('button', { name: 'Выйти' }))
    await waitFor(() => expect(goToLogin).toHaveBeenCalledWith('out'))
    expect(requests.some((r) => r.path === '/api/session/logout' && r.method === 'POST')).toBe(true)
  })

  it('has no session line through the tunnel', async () => {
    stubApi({ ...API, '/api/session': { via: 'local' } })
    renderShell('/vacancies')
    expect(await screen.findByText(/Откликов сегодня:/)).toBeInTheDocument()
    expect(screen.queryByText(/Вход по ссылке/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Выйти' })).toBeNull()
  })
})
