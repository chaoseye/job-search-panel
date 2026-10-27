import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import { ClarifyBlock, DeliveryBlock } from './DashboardPage'

const item = (id: number, name: string) => ({ id, name, employer: { id: '1', name: 'Рога и копыта' }, score: 80 })

describe('ClarifyBlock', () => {
  it('lists the letters waiting for the user and leads to them', async () => {
    const api = stubApi({ '/api/vacancies': { items: [item(7, 'Python-разработчик'), item(9, 'QA-инженер')], total: 12 } })
    renderAt('/', '/', <ClarifyBlock />)

    expect(await screen.findByText('Письма ждут уточнений: 12')).toBeInTheDocument()
    expect(api.calls[0]).toBe('/api/vacancies?group=needs_clarification&sort=score&per_page=5')
    expect(screen.getByRole('link', { name: 'Python-разработчик' })).toHaveAttribute('href', '/vacancies?open=7')

    fireEvent.click(screen.getByRole('link', { name: 'Показать все' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/vacancies?group=needs_clarification'))
  })

  it('stays hidden when nothing waits', async () => {
    const api = stubApi({ '/api/vacancies': { items: [], total: 0 } })
    const { container } = renderAt('/', '/', <ClarifyBlock />)
    await waitFor(() => expect(api.calls).toHaveLength(1))
    expect(container.querySelector('section')).toBeNull()
  })

  it('stays hidden when the request fails', async () => {
    const api = stubApi({})
    const { container } = renderAt('/', '/', <ClarifyBlock />)
    await waitFor(() => expect(api.calls).toHaveLength(1))
    expect(container.querySelector('section')).toBeNull()
  })
})

describe('DeliveryBlock', () => {
  it('warns about messages the bot never got, with the last reason', async () => {
    const api = stubApi({
      '/api/notifications/summary': {
        days: 7,
        failed: 3,
        queued: 0,
        last_failed: { event_type: 'letter', label: 'Вакансия с письмом', error: 'Forbidden: bot was blocked by the user', created_at: null },
      },
    })
    renderAt('/', '/', <DeliveryBlock />)

    expect(await screen.findByText('Не доставлено в Telegram за 7 дней: 3')).toBeInTheDocument()
    expect(screen.getByText(/Вакансия с письмом.*bot was blocked by the user/)).toBeInTheDocument()
    expect(api.calls).toEqual(['/api/notifications/summary'])
    fireEvent.click(screen.getByRole('link', { name: 'Настройки Telegram' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/settings#telegram$/))
  })

  it('stays hidden when everything was delivered', async () => {
    const api = stubApi({ '/api/notifications/summary': { days: 7, failed: 0, queued: 2, last_failed: null } })
    const { container } = renderAt('/', '/', <DeliveryBlock />)
    await waitFor(() => expect(api.calls).toHaveLength(1))
    expect(container.querySelector('section')).toBeNull()
  })
})
