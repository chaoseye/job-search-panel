import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Building2,
  ChartColumn,
  FileSearch,
  Gavel,
  Inbox,
  LayoutDashboard,
  LogOut,
  Menu as MenuIcon,
  MessagesSquare,
  Palette,
  ScrollText,
  Search,
  Send,
  Settings,
  SquareKanban,
  UserRound,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { apiGet, apiPost } from '../api/client'
import { goToLogin } from '../api/login'
import { useSessionInfo } from '../api/session'
import type { ApplyStatus, AuthStatus, BatchesSummary, ChannelsSummary, SettingsView, SitesSummary, StudiosSummary, SyncStatus, TendersSummary, VacancyCounts } from '../api/types'
import { clockTime, relativeTime } from '../lib/format'
import { Count } from './ui/Badge'
import { Button } from './ui/Button'
import { cx } from './ui/cx'
import { Sheet, SheetCloseButton } from './ui/Sheet'
import { Notice } from './ui/States'

type BadgeKey = 'vacancies' | 'applications' | 'batches' | 'channels' | 'sites' | 'studios' | 'tenders'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  end?: boolean
  badge?: BadgeKey
}

/** The 14 sections in four groups by what the user does there; counts only on queues worth opening. */
export const NAV_GROUPS: { label: string; items: NavItem[] }[] = [
  {
    label: 'Работа',
    items: [
      { to: '/', label: 'Сводка', icon: LayoutDashboard, end: true },
      { to: '/board', label: 'Воронка', icon: SquareKanban },
      { to: '/vacancies', label: 'Вакансии', icon: FileSearch, badge: 'vacancies' },
      { to: '/applications', label: 'Отклики', icon: Send, badge: 'applications' },
      { to: '/batches', label: 'Разбор', icon: Inbox, badge: 'batches' },
    ],
  },
  {
    label: 'Другие источники',
    items: [
      { to: '/channels', label: 'Каналы', icon: MessagesSquare, badge: 'channels' },
      { to: '/sites', label: 'Сайты компаний', icon: Building2, badge: 'sites' },
      { to: '/tenders', label: 'Тендеры', icon: Gavel, badge: 'tenders' },
      { to: '/studios', label: 'Студии', icon: Palette, badge: 'studios' },
    ],
  },
  {
    label: 'Отбор',
    items: [
      { to: '/searches', label: 'Поиски', icon: Search },
      { to: '/profile', label: 'Профиль и критерии', icon: UserRound },
    ],
  },
  {
    label: 'Сервис',
    items: [
      { to: '/analytics', label: 'Аналитика', icon: ChartColumn },
      { to: '/logs', label: 'Журнал', icon: ScrollText },
      { to: '/settings', label: 'Настройки', icon: Settings },
    ],
  },
]

/** Queues where the count means «something waits for you» (accent); the others are plain «new» counts. */
const ATTENTION: BadgeKey[] = ['vacancies', 'applications', 'batches']

function useBadges(): Partial<Record<BadgeKey, number>> {
  const every = (ms: number) => ({ refetchInterval: ms })
  const vacancies = useQuery({ queryKey: ['vacancy-summary'], queryFn: () => apiGet<VacancyCounts>('/api/vacancies/summary'), ...every(30000) })
  const batches = useQuery({ queryKey: ['batches-summary'], queryFn: () => apiGet<BatchesSummary>('/api/batches/summary'), ...every(30000) })
  const sync = useQuery({ queryKey: ['sync-status'], queryFn: () => apiGet<SyncStatus>('/api/negotiations/status'), ...every(30000) })
  const channels = useQuery({ queryKey: ['channels-summary'], queryFn: () => apiGet<ChannelsSummary>('/api/channels/summary'), ...every(30000) })
  const sites = useQuery({ queryKey: ['sites-summary'], queryFn: () => apiGet<SitesSummary>('/api/sites/summary'), ...every(30000) })
  const studios = useQuery({ queryKey: ['studios-summary'], queryFn: () => apiGet<StudiosSummary>('/api/studios/summary'), ...every(30000) })
  const tenders = useQuery({ queryKey: ['tenders-summary'], queryFn: () => apiGet<TendersSummary>('/api/tenders/summary'), ...every(60000) })
  return {
    vacancies: vacancies.data?.unseen_candidates,
    applications: sync.data?.with_updates,
    batches: batches.data?.pending_batches,
    channels: channels.data?.new_posts,
    sites: sites.data?.new_matches,
    studios: (studios.data?.drafted ?? 0) + (studios.data?.replied ?? 0),
    tenders: tenders.data?.new_relevant,
  }
}

function NavList({ badges, onNavigate }: { badges: Partial<Record<BadgeKey, number>>; onNavigate?: () => void }) {
  return (
    <nav aria-label="Разделы" className="flex flex-col gap-4 px-3 pb-4">
      {NAV_GROUPS.map((group) => (
        <div key={group.label}>
          <div className="px-2.5 pb-1 text-xs font-medium text-ink-3">{group.label}</div>
          <ul className="flex flex-col gap-px">
            {group.items.map(({ to, label, icon: Icon, end, badge }) => {
              const count = badge ? badges[badge] : undefined
              return (
                <li key={to}>
                  <NavLink
                    to={to}
                    end={end}
                    onClick={onNavigate}
                    className={({ isActive }) =>
                      cx(
                        'flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm transition-colors pointer-coarse:h-10',
                        'focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus',
                        isActive ? 'bg-selected font-medium text-ink' : 'text-ink-2 hover:bg-sunken hover:text-ink',
                      )
                    }
                  >
                    <Icon aria-hidden className="size-4 shrink-0 opacity-80" strokeWidth={1.75} />
                    <span className="min-w-0 flex-1 truncate">{label}</span>
                    {count ? (
                      <>
                        {' '}
                        <Count value={count} attention={ATTENTION.includes(badge!)} />
                      </>
                    ) : null}
                  </NavLink>
                </li>
              )
            })}
          </ul>
        </div>
      ))}
    </nav>
  )
}

const TOKEN_STATE: Record<string, string> = {
  expired: 'токен истёк, войдите заново',
  revoked: 'токен отозван, войдите заново',
}

/** hh.ru sign-in, the token, the apply mode and today's count — what the old top bar said, now under the navigation. */
function StatusFooter({ onNavigate }: { onNavigate?: () => void }) {
  const auth = useQuery({ queryKey: ['auth-status'], queryFn: () => apiGet<AuthStatus>('/api/auth/status') })
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => apiGet<SettingsView>('/api/settings') })
  const apply = useQuery({ queryKey: ['apply-status'], queryFn: () => apiGet<ApplyStatus>('/api/applications/status'), refetchInterval: 30000 })
  const user = auth.data?.user
  const name = user ? `${user.first_name ?? ''} ${user.last_name ?? ''}`.trim() : ''
  const loggedIn = auth.data?.logged_in
  const state = auth.data?.state
  const token = !loggedIn ? null : state === 'ok' ? (auth.data?.expires_at ? `токен истекает ${relativeTime(auth.data.expires_at)}` : null) : (TOKEN_STATE[state ?? ''] ?? `токен: ${state}`)
  return (
    <div className="mt-auto border-t border-line px-5 py-3 text-xs text-ink-3">
      <Link to="/settings" onClick={onNavigate} className="flex items-center gap-2 rounded text-ink-2 hover:text-ink">
        <span aria-hidden className={cx('size-1.5 shrink-0 rounded-full', loggedIn ? (state === 'ok' ? 'bg-good' : 'bg-warn') : 'bg-bad')} />
        <span className="truncate">{auth.data ? (loggedIn ? `hh.ru: ${name || 'вход выполнен'}` : 'hh.ru: вход не выполнен') : 'hh.ru: …'}</span>
      </Link>
      {token && <div className={cx('mt-1 pl-3.5', state !== 'ok' && 'font-medium text-warn')}>{token}</div>}
      {settings.data && <div className="mt-1 pl-3.5">{settings.data.apply_mode === 'api' ? 'Отклики через API' : 'Режим «Ассистент»'}</div>}
      {apply.data && (
        <div className="pl-3.5">
          Откликов сегодня:{' '}
          <span className="tabular-nums">
            {apply.data.limits.today_sent} из {apply.data.limits.max_per_day}
          </span>
        </div>
      )}
      <LinkSession />
    </div>
  )
}

/** Opened through the public address by the bot's link: until when the session lasts, and «Выйти» for this browser
 *  (/logout in the bot ends every session). Through the tunnel there is no session and nothing here. */
function LinkSession() {
  const session = useSessionInfo()
  const logout = useMutation({ mutationFn: () => apiPost('/api/session/logout'), onSettled: () => goToLogin('out') })
  if (session.data?.via !== 'link') return null
  return (
    <div className="mt-2 flex items-center justify-between gap-2 border-t border-line pt-2">
      <span className="min-w-0 truncate">{session.data.expires_at ? `Вход по ссылке до ${clockTime(session.data.expires_at)}` : 'Вход по ссылке'}</span>
      <Button variant="ghost" size="sm" onClick={() => logout.mutate()} disabled={logout.isPending}>
        <LogOut aria-hidden />
        Выйти
      </Button>
    </div>
  )
}

/** Pause and captcha stop all background work: the one message that belongs on every screen. */
function GlobalNotices() {
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => apiGet<SettingsView>('/api/settings') })
  const session = useSessionInfo()
  const reason = settings.data?.paused_reason
  if (!reason) return null
  if (reason === 'captcha')
    return (
      <Notice tone="warn" role="alert" title="hh.ru просит пройти капчу, фоновые задачи на паузе" className="mb-5">
        {settings.data?.captcha_url ? (
          <a className="font-medium text-accent underline underline-offset-2" href={settings.data.captcha_url} target="_blank" rel="noreferrer">
            Пройти капчу
          </a>
        ) : (
          'Откройте hh.ru в браузере и пройдите проверку.'
        )}
        {/* hh returns from the captcha to localhost: away from the laptop that page does not open */}
        {session.data?.via === 'link' && (
          <>
            {' '}
            Отсюда hh.ru не вернёт вас в панель: после капчи снимите паузу в{' '}
            <Link className="font-medium text-accent underline underline-offset-2" to="/logs">
              «Журнале»
            </Link>
            .
          </>
        )}
      </Notice>
    )
  return (
    <Notice tone="warn" role="status" title="Фоновые задачи на паузе" className="mb-5">
      {reason === 'user' ? 'Пауза включена вручную.' : `Причина: ${reason}.`} Снять её можно в{' '}
      <Link className="font-medium text-accent underline underline-offset-2" to="/logs">
        «Журнале»
      </Link>{' '}
      или командой /resume в боте.
    </Notice>
  )
}

function currentLabel(path: string): string {
  for (const group of NAV_GROUPS)
    for (const item of group.items)
      if (item.end ? path === item.to : path === item.to || path.startsWith(`${item.to}/`)) return item.label
  return 'Job Search Panel'
}

/** Application frame: the grouped navigation on the left (a menu sheet on narrow screens), the page on the canvas. */
export default function AppShell() {
  const badges = useBadges()
  const location = useLocation()
  const [menuOpen, setMenuOpen] = useState(false)
  useEffect(() => setMenuOpen(false), [location.pathname])
  return (
    <div className="min-h-dvh bg-canvas font-ui text-ink antialiased">
      <div className="lg:flex">
        <aside className="sticky top-0 hidden h-dvh w-[236px] shrink-0 flex-col overflow-y-auto border-r border-line lg:flex">
          <Link to="/" className="mx-5 mb-4 mt-5 block rounded text-[15px] font-semibold tracking-[-0.01em] text-ink">
            Job Search Panel
          </Link>
          <NavList badges={badges} />
          <StatusFooter />
        </aside>

        <div className="min-w-0 flex-1">
          <header className="sticky top-0 z-30 flex h-12 items-center gap-1 border-b border-line bg-surface px-2 lg:hidden">
            <Button variant="ghost" iconOnly onClick={() => setMenuOpen(true)} aria-label="Открыть меню разделов">
              <MenuIcon aria-hidden />
            </Button>
            <span className="truncate text-[15px] font-semibold">{currentLabel(location.pathname)}</span>
          </header>
          <main className="mx-auto w-full max-w-[1600px] px-4 py-5 sm:px-6 lg:px-8 lg:py-7">
            <GlobalNotices />
            <Outlet />
          </main>
        </div>
      </div>

      {menuOpen && (
        <Sheet side="left" size="nav" labelledBy="nav-sheet-title" onClose={() => setMenuOpen(false)}>
          <div className="flex h-12 items-center justify-between border-b border-line pl-5 pr-2">
            <span id="nav-sheet-title" className="text-[15px] font-semibold">
              Job Search Panel
            </span>
            <SheetCloseButton label="Закрыть меню" />
          </div>
          {/* a tap on a link closes the menu even when it leads to the page already open */}
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto pt-4">
            <NavList badges={badges} onNavigate={() => setMenuOpen(false)} />
            <StatusFooter onNavigate={() => setMenuOpen(false)} />
          </div>
        </Sheet>
      )}
    </div>
  )
}
