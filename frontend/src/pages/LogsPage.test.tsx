import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import LogsPage from './LogsPage'

const API = {
  '/api/health': { version: '0.1.0', apply_mode: 'assist', paused_reason: null, scheduler_jobs: ['scan_tick', 'responses_refresh'] },
  '/api/jobs': { running: [], runs: [], last_scan_at: null, last_sync_at: null, paused_reason: null, failures: {} },
  '/api/audit': [],
  '/api/jobs/responses': { job_run_id: 7 },
}

describe('LogsPage', () => {
  it('runs the refresh of the responses counter from «Запустить сейчас»', async () => {
    const api = stubApi(API)
    renderAt('/logs', '/logs', <LogsPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Обновить число откликов' }))
    await waitFor(() => expect(api.requests.some((r) => r.path === '/api/jobs/responses' && r.method === 'POST')).toBe(true))
  })

  it('keeps the button off while the pass runs', async () => {
    stubApi({ ...API, '/api/jobs': { ...API['/api/jobs'], running: ['responses'] } })
    renderAt('/logs', '/logs', <LogsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Обновить число откликов' })).toBeDisabled())
    expect(screen.getByRole('button', { name: 'Проверить закрытые' })).toBeEnabled()
  })
})
