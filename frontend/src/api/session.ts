import { useQuery } from '@tanstack/react-query'
import { apiGet } from './client'

/** How this browser reached the panel: `local` — the laptop or the SSH tunnel, as always; `link` — the public address
 *  with a session from the bot's one-time link (/open), which ends at `expires_at`, on «Выйти» or on /logout. */
export type SessionInfo = { via: 'local' } | { via: 'link'; created_at: string | null; expires_at: string | null }

export function useSessionInfo() {
  return useQuery({ queryKey: ['session'], queryFn: () => apiGet<SessionInfo>('/api/session'), staleTime: 300_000 })
}
