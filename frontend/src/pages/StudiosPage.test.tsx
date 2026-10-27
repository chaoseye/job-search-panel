import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import StudiosPage, { mailtoHref } from './StudiosPage'

const studio = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  site_url: `https://${name.toLowerCase()}.example`,
  domain: `${name.toLowerCase()}.example`,
  hh_employer_id: null,
  hh_url: null,
  area: 'Москва',
  kind: 'dev',
  kind_label: 'разработка',
  source: 'list',
  sources: ['list'],
  letter_kind: 'application',
  offer_allowed: true,
  open_vacancies: null,
  services: ['веб-разработка'],
  emails: [`hello@${name.toLowerCase()}.example`],
  telegram: null,
  contact_url: null,
  contacts_checked_at: '2026-09-24T10:00:00+00:00',
  contacts_error: null,
  fit_score: 40,
  fit_notes: ['совпадает: Python'],
  status: 'ready',
  status_label: 'есть контакты',
  sent_at: null,
  sent_to: null,
  sent_via: null,
  replied_at: null,
  reply_subject: null,
  first_seen_at: null,
  notes: null,
  ...extra,
})

const LIST = {
  items: [studio(7, 'Pixel'), studio(8, 'Kod')],
  total: 2,
  page: 0,
  per_page: 50,
  counts: { by_status: { ready: 2 }, work: 2, ready: 2, drafted: 0, sent: 0, replied: 0, closed: 0, all: 2 },
  sent_today: 3,
  daily_limit: 20,
}

const letter = (id: number, style: 'business' | 'simple', subject: string, text: string, isCurrent: boolean) => ({
  id,
  version: id,
  kind: 'application',
  style,
  style_label: style === 'business' ? 'деловой' : 'простой',
  subject,
  text,
  full_text: `${text}\n\nИван`,
  origin: 'claude',
  batch_id: 'b',
  needs_clarification: [],
  warnings: [],
  is_current: isCurrent,
  created_at: null,
})
const BUSINESS = letter(1, 'business', 'Разработчик для Pixel', 'Здравствуйте! Текст письма.', true)
const SIMPLE = letter(2, 'simple', 'Разработчик для Pixel', 'Здравствуйте! Пишу backend и тесты.', false)

const DETAIL = {
  ...studio(7, 'Pixel', {
    hh_employer_id: '11',
    hh_url: 'https://hh.ru/employer/11',
    sources: ['hh_search'],
    offer_allowed: false,
    status: 'drafted',
    status_label: 'письмо готово',
  }),
  description: 'Веб-студия',
  site_summary: 'Сайты и приложения',
  letter: BUSINESS,
  variants: [BUSINESS, SIMPLE],
  letters: [SIMPLE, BUSINESS],
  request: null,
  vacancies: [{ id: 55, name: 'Python-разработчик', status: 'suitable', score: 80 }],
}

const routes = {
  '/api/studios/settings': { keywords: ['веб-студия'], only_with_vacancies: true, signature: 'Иван', offer_notes: '' },
  '/api/studios/letters': { queued: 1, skipped: [], batch_id: '20260924_1200_abcd' },
  '/api/studios/7': DETAIL,
  '/api/studios': LIST,
  '/api/jobs': { running: [], runs: [], last_scan_at: null, last_sync_at: null },
}

describe('StudiosPage', () => {
  it('lists the studios and queues letters for the chosen ones', async () => {
    const api = stubApi(routes)
    renderAt('/studios', '/studios', <StudiosPage />)
    expect(await screen.findByText('Pixel')).toBeInTheDocument()
    expect(screen.getByText('Отправлено сегодня: 3 из 20')).toBeInTheDocument()
    expect(api.calls.some((c) => c.startsWith('/api/studios?group=work&sort=fit'))).toBe(true)

    fireEvent.click(screen.getByRole('checkbox', { name: 'Выбрать: Pixel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Подготовить письма (1)' }))
    await waitFor(() => expect(api.calls).toContain('/api/studios/letters'))
    expect(await screen.findByText(/пакет 20260924_1200_abcd, запустите \/hh-batch/)).toBeInTheDocument()
  })

  it('opens a studio: the letter goes out from the user’s own mail, no sales offer to an hh studio', async () => {
    stubApi(routes)
    renderAt('/studios', '/studios', <StudiosPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Pixel' }))
    const open = await screen.findByRole('link', { name: 'Открыть в почте' })
    expect(open.getAttribute('href')).toBe(mailtoHref('hello@pixel.example', 'Разработчик для Pixel', 'Здравствуйте! Текст письма.\n\nИван'))
    const offer = screen.getByRole('option', { name: /предложение услуг — нельзя для студий с hh.ru/ })
    expect(offer).toBeDisabled()
    expect(screen.getByRole('link', { name: 'Python-разработчик' })).toHaveAttribute('href', '/vacancies?open=55')
  })

  it('shows the two drafts; mail, edits and «sent» work with the one on screen', async () => {
    const api = stubApi(routes)
    renderAt('/studios', '/studios', <StudiosPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Pixel' }))
    const business = await screen.findByRole('tab', { name: 'Деловой' })
    expect(business).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('Текст письма')).toHaveValue('Здравствуйте! Текст письма.')

    fireEvent.click(screen.getByRole('tab', { name: 'Простой' }))
    expect(screen.getByRole('tab', { name: 'Простой' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('Тема письма')).toHaveValue('Разработчик для Pixel')
    expect(screen.getByLabelText('Текст письма')).toHaveValue('Здравствуйте! Пишу backend и тесты.')
    expect(screen.getByRole('link', { name: 'Открыть в почте' }).getAttribute('href')).toBe(
      mailtoHref('hello@pixel.example', 'Разработчик для Pixel', 'Здравствуйте! Пишу backend и тесты.\n\nИван'),
    )

    fireEvent.change(screen.getByLabelText('Текст письма'), { target: { value: 'Здравствуйте! Пишу backend, тесты и CI.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить правку' }))
    await waitFor(() => expect(api.requests.some((r) => r.method === 'PUT' && r.path === '/api/studios/7/letter')).toBe(true))
    expect(api.requests.find((r) => r.path === '/api/studios/7/letter')?.body).toEqual({
      subject: 'Разработчик для Pixel',
      text: 'Здравствуйте! Пишу backend, тесты и CI.',
      style: 'simple',
    })

    fireEvent.click(screen.getByRole('button', { name: 'Отметить отправленным' }))
    await waitFor(() => expect(api.calls).toContain('/api/studios/7/sent'))
    expect(api.requests.find((r) => r.path === '/api/studios/7/sent')?.body).toEqual({ to: 'hello@pixel.example', via: 'email', letter_id: 2 })
  })
})

describe('mailtoHref', () => {
  it('encodes the address, the subject and the body', () => {
    expect(mailtoHref('a@b.example', 'Тема & вопрос', 'строка 1\nстрока 2')).toBe(
      'mailto:a%40b.example?subject=%D0%A2%D0%B5%D0%BC%D0%B0%20%26%20%D0%B2%D0%BE%D0%BF%D1%80%D0%BE%D1%81&body=%D1%81%D1%82%D1%80%D0%BE%D0%BA%D0%B0%201%0A%D1%81%D1%82%D1%80%D0%BE%D0%BA%D0%B0%202',
    )
  })
})
