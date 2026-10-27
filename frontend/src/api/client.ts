import { goToLogin } from './login'

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
    this.status = status
    this.detail = detail
  }
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { detail?: unknown } | string | undefined
    if (typeof d === 'string') return d
    if (d && typeof d === 'object' && 'detail' in d) {
      const inner = (d as { detail?: unknown }).detail
      if (typeof inner === 'string') return inner
      return JSON.stringify(inner)
    }
    return `HTTP ${err.status}`
  }
  if (err instanceof Error) return err.message
  return String(err)
}

/** The error of a failed response. Through the public address a session that has ended (12 hours, «Выйти» in another
 *  tab, /logout in the bot) answers 401 `login_required`: the page then goes to the sign-in instead of showing an
 *  error in every block. */
async function failure(res: Response): Promise<ApiError> {
  let detail: unknown = undefined
  try {
    detail = await res.json()
  } catch {
    detail = await res.text().catch(() => undefined)
  }
  if (res.status === 401 && detail && typeof detail === 'object' && (detail as { kind?: unknown }).kind === 'login_required') goToLogin('expired')
  return new ApiError(res.status, detail)
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) throw await failure(res)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export async function apiUpload<T>(path: string, field: string, file: File): Promise<T> {
  const form = new FormData()
  form.append(field, file, file.name)
  const res = await fetch(path, { method: 'POST', body: form })
  if (!res.ok) throw await failure(res)
  return (await res.json()) as T
}

export async function apiText(path: string): Promise<string> {
  const res = await fetch(path)
  if (!res.ok) throw await failure(res)
  return res.text()
}

export const apiGet = <T,>(path: string) => api<T>(path)
export const apiDelete = <T,>(path: string) => api<T>(path, { method: 'DELETE' })
export const apiPost = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
export const apiPut = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) })
