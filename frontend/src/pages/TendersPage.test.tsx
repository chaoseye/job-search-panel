import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import TendersPage, { deadlineText } from './TendersPage'

const tender = (id: number, title: string, extra: Record<string, unknown> = {}) => ({
  id,
  ws_id: 18900 + id,
  url: `https://workspace.ru/tenders/t-${18900 + id}/`,
  title,
  category: 'crm',
  section: 'CRM, 1С, ПО, чат-боты',
  service: 'Чат-боты',
  budget: 'до 150 000',
  budget_max: 150000,
  published_on: '2026-09-24',
  deadline_on: '2099-12-30',
  state: 'open',
  open: true,
  city: 'Ульяновска',
  organizer: 'Юридическое лицо',
  participants: 3,
  views: 120,
  fit_score: 60,
  fit_notes: ['совпадает: бот'],
  relevant: true,
  status: 'new',
  status_label: 'новый',
  detail_error: null,
  first_seen_at: null,
  responded_at: null,
  notes: null,
  ...extra,
})

const LIST = {
  items: [tender(1, 'Telegram-бот для записи'), tender(2, 'Сборка MVP', { status: 'drafted', status_label: 'отклик готов' })],
  total: 2,
  page: 0,
  per_page: 50,
  counts: { relevant: 2, open: 5, drafted: 1, responded: 0, closed: 0, all: 7 },
}

const DETAIL = {
  ...tender(2, 'Сборка MVP', { status: 'drafted', status_label: 'отклик готов' }),
  description: 'Нужен лендинг и анкета.\nВ отклике просим указать:\n— срок;',
  conditions: [{ title: 'Требуемая услуга', value: 'Разработка сайтов под ключ' }],
  reply: {
    id: 5,
    version: 1,
    text: 'Здравствуйте! Соберу лендинг за пять дней.',
    origin: 'claude',
    batch_id: 'b',
    needs_clarification: ['[[УТОЧНИТЬ: стоимость]]'],
    warnings: [],
    is_current: true,
    created_at: null,
  },
  replies: [],
  request: null,
}

const routes = {
  '/api/tenders/settings': {
    sections: ['web-development', 'crm'],
    min_score: 30,
    rates: '',
    available_sections: [
      { key: 'web-development', label: 'Разработка сайтов' },
      { key: 'crm', label: 'CRM, 1С, ПО, чат-боты' },
    ],
  },
  '/api/tenders/replies': { queued: 1, skipped: [], batch_id: '20260924_1200_abcd' },
  '/api/tenders/2': DETAIL,
  '/api/tenders': LIST,
  '/api/jobs': { running: [], runs: [], last_scan_at: null, last_sync_at: null },
}

describe('TendersPage', () => {
  it('lists the suitable tenders and asks Claude for answers to the chosen ones', async () => {
    const api = stubApi(routes)
    renderAt('/tenders', '/tenders', <TendersPage />)
    expect(await screen.findByText('Telegram-бот для записи')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Подходящие 2', selected: true })).toBeInTheDocument()
    expect(api.calls.some((c) => c.startsWith('/api/tenders?group=relevant&sort=fit'))).toBe(true)
    expect(screen.getAllByText(/заказчик из Ульяновска/).length).toBe(2)

    fireEvent.click(screen.getByRole('checkbox', { name: 'Выбрать: Telegram-бот для записи' }))
    fireEvent.click(screen.getByRole('button', { name: 'Подготовить отклики (1)' }))
    await waitFor(() => expect(api.calls).toContain('/api/tenders/replies'))
    expect(api.requests.find((r) => r.path === '/api/tenders/replies')?.body).toEqual({ ids: [1] })
    expect(await screen.findByText(/пакет 20260924_1200_abcd, запустите \/hh-batch/)).toBeInTheDocument()
  })

  it('opens a tender: the answer is edited, copied and posted on workspace.ru by the user', async () => {
    const api = stubApi(routes)
    renderAt('/tenders', '/tenders', <TendersPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Сборка MVP' }))
    expect(await screen.findByLabelText('Текст отклика')).toHaveValue('Здравствуйте! Соберу лендинг за пять дней.')
    expect(screen.getByText('Уточните перед отправкой')).toBeInTheDocument()
    expect(screen.getByText('[[УТОЧНИТЬ: стоимость]]')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Откликнуться на workspace.ru' })).toHaveAttribute('href', 'https://workspace.ru/tenders/t-18902/')

    fireEvent.change(screen.getByLabelText('Текст отклика'), { target: { value: 'Здравствуйте! Соберу лендинг за четыре дня.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить правку' }))
    await waitFor(() => expect(api.requests.some((r) => r.method === 'PUT' && r.path === '/api/tenders/2/reply')).toBe(true))
    expect(api.requests.find((r) => r.path === '/api/tenders/2/reply')?.body).toEqual({ text: 'Здравствуйте! Соберу лендинг за четыре дня.' })

    fireEvent.click(screen.getByRole('button', { name: 'Я откликнулся' }))
    await waitFor(() => expect(api.calls).toContain('/api/tenders/2/responded'))
  })
})

describe('TendersPage: the organizer\'s answer', () => {
  it('records the answer of a responded tender in its card and shows when it was recorded', async () => {
    const responded = {
      ...DETAIL,
      status: 'responded',
      status_label: 'откликнулся',
      responded_at: '2026-10-24T10:00:00Z',
      answered_at: null,
    }
    const api = stubApi({ ...routes, '/api/tenders/2': responded })
    renderAt('/tenders', '/tenders', <TendersPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Сборка MVP' }))
    expect(await screen.findByRole('button', { name: 'Ответили' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Я откликнулся' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Отказ' }))
    await waitFor(() => expect(api.calls).toContain('/api/tenders/2/declined'))
    fireEvent.click(screen.getByRole('button', { name: 'Ответили' }))
    await waitFor(() => expect(api.calls).toContain('/api/tenders/2/replied'))
  })
})

describe('deadlineText', () => {
  const today = new Date(2026, 8, 24)
  it('shows the date and the days left for an open tender', () => {
    expect(deadlineText('2026-09-30', true, today)).toBe('30.09 · 6 дн.')
    expect(deadlineText('2026-09-25', true, today)).toBe('25.09 · завтра')
    expect(deadlineText('2026-09-24', true, today)).toBe('24.09 · сегодня')
    expect(deadlineText('2026-09-20', true, today)).toBe('20.09')
    expect(deadlineText('2026-09-30', false, today)).toBe('30.09')
    expect(deadlineText(null, true, today)).toBe('—')
  })
})
