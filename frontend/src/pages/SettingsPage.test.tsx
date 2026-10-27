import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import SettingsPage from './SettingsPage'

const selected = (name: string) => expect(screen.getByRole('tab', { name })).toHaveAttribute('aria-selected', 'true')

describe('SettingsPage', () => {
  it('opens the section named in the address and keeps the chosen one there', async () => {
    const api = stubApi({})
    renderAt('/settings#telegram', '/settings', <SettingsPage />)
    selected('Telegram')
    await waitFor(() => expect(api.calls).toContain('/api/notifications/telegram'))

    fireEvent.click(screen.getByRole('tab', { name: 'Почта' }))
    selected('Почта')
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/settings#mail$/))

    // the first section is the page itself: no hash
    fireEvent.click(screen.getByRole('tab', { name: 'Аккаунт и API' }))
    selected('Аккаунт и API')
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/settings$/))
  })

  it('falls back to the first section for an unknown hash', () => {
    stubApi({})
    renderAt('/settings#nothing', '/settings', <SettingsPage />)
    selected('Аккаунт и API')
  })
})
