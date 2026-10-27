import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import TelegramSection from './TelegramSection'

const STATUS = {
  token_set: true,
  chat_id: '42',
  chat_id_source: 'env',
  configured: true,
  bot_username: 'ExamplePanelBot',
  bot_name: 'HH',
  bot_link: 'https://t.me/ExamplePanelBot',
  daily_summary_at: '21:00',
  resume_bump: { at: [], days: '', label: null, event_enabled: true },
  hot_digest_at: null,
  letters: { enabled: true, min_score: 50, event_in_env: true },
  stop_words: ['продаж', 'курьер'],
  events: [{ id: 'search_new', label: 'Вакансия по поиску-наблюдению', enabled: true }],
  queue: { sent: 3 },
  last_sent_at: null,
}

describe('TelegramSection', () => {
  it('keeps vacancies with the stop words out of the bot: the list is edited and saved', async () => {
    const api = stubApi({
      '/api/notifications/telegram/stop-words': { stop_words: ['продаж', 'курьер', 'кассир'] },
      '/api/notifications/telegram': STATUS,
      '/api/notifications': [],
    })
    renderAt('/settings', '/settings', <TelegramSection />)
    expect(await screen.findByText('продаж')).toBeInTheDocument()
    expect(screen.getByText('курьер')).toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Сохранить слова' })
    expect(save).toBeDisabled()

    const input = screen.getByRole('button', { name: 'Удалить курьер' }).closest('div')!.querySelector('input')!
    fireEvent.change(input, { target: { value: 'кассир' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText('кассир')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить слова' }))
    await waitFor(() => expect(api.calls).toContain('/api/notifications/telegram/stop-words'))
    expect(api.requests.find((r) => r.path === '/api/notifications/telegram/stop-words')?.body).toEqual({
      words: ['продаж', 'курьер', 'кассир'],
    })
  })
})
