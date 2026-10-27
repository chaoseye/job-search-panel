import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, apiGet, apiText } from './client'
import { goToLogin } from './login'

vi.mock('./login', () => ({ goToLogin: vi.fn() }))

function answer(status: number, body: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })))
}

describe('api client', () => {
  beforeEach(() => vi.mocked(goToLogin).mockClear())

  it('goes to the sign-in when the session of the public address has ended', async () => {
    answer(401, { detail: 'Нужен вход', kind: 'login_required' })
    await expect(apiGet('/api/vacancies')).rejects.toBeInstanceOf(ApiError)
    await expect(apiText('/api/profile/md')).rejects.toMatchObject({ status: 401 })
    expect(goToLogin).toHaveBeenCalledTimes(2)
    expect(goToLogin).toHaveBeenCalledWith('expired')
  })

  it('leaves other errors to the page', async () => {
    answer(401, { detail: 'чужое' })
    await expect(apiGet('/api/settings')).rejects.toMatchObject({ status: 401 })
    answer(403, { detail: 'Запрос с чужого сайта отклонён' })
    await expect(apiGet('/api/settings')).rejects.toMatchObject({ status: 403 })
    expect(goToLogin).not.toHaveBeenCalled()
  })
})
