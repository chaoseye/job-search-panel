import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import ApplicationsPage from './ApplicationsPage'

const HOUR = 3600000
const ago = (hours: number) => new Date(Date.now() - hours * HOUR).toISOString()

const negotiation = {
  hh_id: '501',
  state_id: 'response',
  state_label: 'Просмотрен',
  hidden: false,
  viewed_by_opponent: true,
  has_updates: false,
  messaging_status: null,
  unread_count: 0,
  messages_count: 0,
  chat_id: null,
  hh_created_at: null,
  hh_updated_at: ago(5),
  first_viewed_at: ago(72),
  first_reaction_at: null,
  invited_at: null,
  discarded_at: null,
  last_employer_message_at: null,
  messages_synced_at: null,
  last_synced_at: null,
  manual: true,
  page_url: null,
}

const row = (id: number, extra: object = {}) => ({
  id,
  vacancy_id: id,
  vacancy: { id, hh_id: String(id), name: `Вакансия ${id}`, employer_name: 'Рога и копыта', alternate_url: null, apply_alternate_url: null, pipeline_status: 'applied', score: 70 },
  mode: 'assist',
  mode_label: 'вручную на hh.ru',
  status: 'sent',
  status_label: 'отправлен',
  letter_text: null,
  queued_at: ago(100),
  sent_at: ago(96),
  hh_negotiation_id: null,
  negotiation_url: null,
  response_url: null,
  error_type: null,
  error_value: null,
  error_text: null,
  attempts: 1,
  confirmed_by_user: true,
  dry_run: false,
  autopilot: false,
  created_at: ago(100),
  negotiation: null,
  ...extra,
})

const sortedBy = (calls: string[], sort: string, order: string) => calls.some((c) => c.startsWith('/api/applications?') && c.includes(`sort=${sort}&order=${order}`))

// the list answers by prefix, so the status routes need their own answers
const STATUS = {
  '/api/applications/status': {
    apply_mode: 'assist',
    consent_accepted_at: '2026-10-01T10:00:00Z',
    resume_id: null,
    dry_run: false,
    autopilot: false,
    autopilot_min_score: 70,
    limits: {
      today_sent: 0,
      max_per_day: 30,
      remaining_day: 30,
      hour_sent: 0,
      max_per_hour: 8,
      remaining_hour: 8,
      window: '09:00-21:00',
      in_window: true,
      paused_until: null,
      paused_reason: null,
      hh_cap: 200,
      can_send: true,
      blocked_reasons: [],
    },
    job_running: false,
  },
  '/api/negotiations/status': { last_sync_at: null, capability: 'unavailable', chats_capability: 'unknown', negotiations: 1, with_updates: 0, job_running: false, interval_min: 15 },
}

describe('ApplicationsPage', () => {
  it('shows when the employer viewed and updated a response under its status', async () => {
    stubApi({ ...STATUS, '/api/applications': { items: [row(1, { negotiation }), row(2)], total: 2, page: 0, per_page: 50, counts: {} } })
    renderAt('/applications', '/applications', <ApplicationsPage />)

    // the table and the phone list are both in the document here (no stylesheet hides one of them)
    expect(await screen.findAllByText('просмотрен 3 дня назад')).not.toHaveLength(0)
    expect(screen.getAllByText('обновлён на hh.ru 5 часов назад')).not.toHaveLength(0)
    expect(screen.queryByRole('columnheader', { name: /Просмотрен|Обновлён/ })).toBeNull()
  })

  it('sorts from the menu by any date; choosing the same order again turns it around', async () => {
    const api = stubApi({ ...STATUS, '/api/applications': { items: [row(1)], total: 1, page: 0, per_page: 50, counts: {} } })
    renderAt('/applications', '/applications', <ApplicationsPage />)
    await screen.findAllByText('Вакансия 1')
    expect(sortedBy(api.calls, 'sent', 'desc')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Дата отправки, по убыванию' }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Просмотр' }))
    await waitFor(() => expect(sortedBy(api.calls, 'viewed', 'desc')).toBe(true))

    fireEvent.click(screen.getByRole('button', { name: 'Просмотр, по убыванию' }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Просмотр' }))
    await waitFor(() => expect(sortedBy(api.calls, 'viewed', 'asc')).toBe(true))
    expect(screen.getByRole('button', { name: 'Просмотр, по возрастанию' })).toBeInTheDocument()
  })
})
