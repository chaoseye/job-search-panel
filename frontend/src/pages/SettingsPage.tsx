import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LogIn } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { apiGet, apiPost, apiPut } from '../api/client'
import { useSessionInfo } from '../api/session'
import type { ApplyStatus, AuthStatus, BackupsView, ProbeReport, SettingsView } from '../api/types'
import MailSection from '../components/MailSection'
import ProbeTable, { CapabilityBadge } from '../components/ProbeTable'
import TelegramSection from '../components/TelegramSection'
import { Badge } from '../components/ui/Badge'
import { Button, buttonClass } from '../components/ui/Button'
import { cx } from '../components/ui/cx'
import { Checkbox, Field, Input, Select } from '../components/ui/Field'
import { Dl } from '../components/ui/Dl'
import { PageHeader } from '../components/ui/PageHeader'
import { Panel } from '../components/ui/Panel'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { Tabs } from '../components/ui/Tabs'
import { formatDateTime, relativeTime } from '../lib/format'

const AUTH_MESSAGES: Record<string, string> = {
  ok: 'Вход в hh.ru выполнен.',
  access_denied: 'Вы отклонили доступ приложения.',
  bad_state: 'Ответ hh.ru не совпал с запросом входа. Попробуйте ещё раз.',
  expired: 'Сессия входа устарела. Попробуйте ещё раз.',
  not_applicant: 'Вход выполнен не как соискатель. Войдите аккаунтом соискателя.',
  transport: 'Не удалось связаться с hh.ru для обмена кода.',
}

/** The sections of the page; the address hash names one (/settings#telegram from «Сводка» opens Telegram). */
const TABS: [string, string][] = [
  ['account', 'Аккаунт и API'],
  ['apply', 'Отклики'],
  ['telegram', 'Telegram'],
  ['mail', 'Почта'],
  ['backups', 'Резервные копии'],
]

export default function SettingsPage() {
  const qc = useQueryClient()
  const location = useLocation()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const fromHash = () => {
    const h = location.hash.slice(1)
    return TABS.some(([id]) => id === h) ? h : 'account'
  }
  const [tab, setTab] = useState(fromHash)
  useEffect(() => setTab(fromHash()), [location.hash]) // eslint-disable-line react-hooks/exhaustive-deps
  const auth = useQuery({ queryKey: ['auth-status'], queryFn: () => apiGet<AuthStatus>('/api/auth/status') })
  const settings = useQuery({ queryKey: ['settings'], queryFn: () => apiGet<SettingsView>('/api/settings') })
  const report = useQuery({ queryKey: ['probe-report'], queryFn: () => apiGet<ProbeReport | null>('/api/probe/report') })

  const authParam = params.get('auth')
  const authReason = params.get('reason')
  const captchaParam = params.get('captcha')

  const dismissAuthParam = () => {
    params.delete('auth')
    params.delete('reason')
    params.delete('captcha')
    setParams(params, { replace: true })
  }
  const choose = (id: string) => {
    setTab(id)
    navigate({ search: location.search, hash: id === 'account' ? '' : id }, { replace: true })
  }

  return (
    <div className="flex flex-col gap-5">
      <PageHeader title="Настройки" meta="Вход в hh.ru, режим откликов, уведомления, почта и резервные копии. Секреты задаются только в .env." />

      {authParam && (
        <Notice
          tone={authParam === 'ok' ? 'good' : 'bad'}
          role="status"
          action={
            <Button variant="ghost" size="sm" onClick={dismissAuthParam}>
              Скрыть
            </Button>
          }
        >
          {AUTH_MESSAGES[authParam === 'ok' ? 'ok' : (authReason ?? '')] ?? `Ошибка входа: ${authReason ?? 'неизвестно'}`}
        </Notice>
      )}
      {captchaParam === 'done' && (
        <Notice
          tone="good"
          role="status"
          action={
            <Button variant="ghost" size="sm" onClick={dismissAuthParam}>
              Скрыть
            </Button>
          }
        >
          Капча пройдена, пауза снята.
        </Notice>
      )}

      <Tabs label="Разделы настроек" value={tab} onChange={choose} items={TABS.map(([id, label]) => ({ id, label }))} />

      {tab === 'account' && (
        <>
          <AccountSection auth={auth.data} loading={auth.isLoading} error={auth.error} onRetry={() => void auth.refetch()} />
          <ProbeSection
            loggedIn={Boolean(auth.data?.logged_in)}
            settings={settings.data}
            report={report.data ?? null}
            onDone={() => {
              void qc.invalidateQueries({ queryKey: ['probe-report'] })
              void qc.invalidateQueries({ queryKey: ['settings'] })
            }}
          />
        </>
      )}
      {tab === 'apply' && <ApplySection />}
      {tab === 'telegram' && <TelegramSection />}
      {tab === 'mail' && <MailSection />}
      {tab === 'backups' && <BackupsSection />}
    </div>
  )
}

function AccountSection({ auth, loading, error, onRetry }: { auth: AuthStatus | undefined; loading: boolean; error: unknown; onRetry: () => void }) {
  const qc = useQueryClient()
  // hh.ru returns from its sign-in to the Redirect URI on localhost: from the public address that page is out of reach
  const remote = useSessionInfo().data?.via === 'link'
  const logout = useMutation({
    mutationFn: () => apiPost<{ ok: boolean }>('/api/auth/logout'),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['auth-status'] })
      void qc.invalidateQueries({ queryKey: ['settings'] })
    },
  })
  const refreshMe = useMutation({
    mutationFn: () => apiPost('/api/auth/refresh-me'),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['auth-status'] }),
  })
  return (
    <Panel
      title="Аккаунт hh.ru"
      meta="Redirect URI ниже должен точно совпадать с указанным в настройках приложения на dev.hh.ru/admin."
      actions={
        auth &&
        (auth.logged_in ? (
          <>
            <Button variant="ghost" onClick={() => refreshMe.mutate()} disabled={refreshMe.isPending}>
              Проверить токен
            </Button>
            <a className={cx(buttonClass('secondary'), remote && 'pointer-events-none opacity-50')} aria-disabled={remote || undefined} href="/api/auth/hh/login">
              Войти заново
            </a>
            <Button variant="danger" onClick={() => logout.mutate()} disabled={logout.isPending}>
              Выйти
            </Button>
          </>
        ) : (
          <a
            className={cx(buttonClass('primary'), (!auth.client_id_set || remote) && 'pointer-events-none opacity-50')}
            aria-disabled={!auth.client_id_set || remote || undefined}
            href="/api/auth/hh/login"
          >
            <LogIn aria-hidden />
            Войти через hh.ru
          </a>
        ))
      }
    >
      {loading && <Skeleton className="h-28 w-full" />}
      {error ? <ErrorNotice error={error} title="Состояние входа не загрузилось" onRetry={onRetry} /> : null}
      {auth && (
        <div className="space-y-4">
          {auth.user_agent_placeholder && (
            <Notice tone="bad" title="В HH_USER_AGENT нужна настоящая почта для связи">
              hh.ru отклоняет заглушки (ошибка bad_user_agent/blacklisted), и запросы к API не работают. Исправьте .env и перезапустите панель.
            </Notice>
          )}
          {remote && (
            <Notice title="Войти в hh.ru можно только с ноутбука, через ярлык Job Search Panel">
              После входа hh.ru возвращает на адрес из Redirect URI, а это localhost ноутбука: с публичного адреса туда не попасть.
            </Notice>
          )}
          {!auth.client_id_set && (
            <Notice tone="warn" title="Не заданы HH_CLIENT_ID и HH_CLIENT_SECRET">
              Заполните их в .env и перезапустите панель.
            </Notice>
          )}
          <Dl
            rows={[
              ['Статус', auth.logged_in ? <Badge tone="good">вход выполнен ({auth.state})</Badge> : <Badge>не выполнен</Badge>],
              ...(auth.user
                ? ([
                    [
                      'Пользователь',
                      <>
                        {`${auth.user.first_name ?? ''} ${auth.user.last_name ?? ''}`.trim() || '—'}
                        {auth.user.email && <span className="text-ink-3"> · {auth.user.email}</span>}{' '}
                        {auth.user.is_applicant ? <Badge tone="good">соискатель</Badge> : <Badge tone="bad">не соискатель</Badge>}
                      </>,
                    ],
                  ] as [string, ReactNode][])
                : []),
              ['Токен действует до', auth.expires_at ? `${formatDateTime(auth.expires_at)} (${relativeTime(auth.expires_at)})` : '—'],
              ['Redirect URI', <span className="break-all font-mono text-xs">{auth.redirect_uri}</span>],
              ['User-Agent', <span className="break-all font-mono text-xs">{auth.user_agent}</span>],
            ]}
          />
          {refreshMe.error && <ErrorNotice error={refreshMe.error} title="Токен не проверен" />}
          {logout.error && <ErrorNotice error={logout.error} title="Выйти не удалось" />}
        </div>
      )}
    </Panel>
  )
}

function BackupsSection() {
  const qc = useQueryClient()
  const backups = useQuery({ queryKey: ['backups'], queryFn: () => apiGet<BackupsView>('/api/backups') })
  const make = useMutation({
    mutationFn: () => apiPost<{ name: string; size_bytes: number; removed: string[] }>('/api/backups'),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['backups'] }),
  })
  const b = backups.data
  return (
    <Panel
      title="Резервные копии"
      meta={`Копия базы panel.db делается ежедневно в ${b?.daily_at ?? '03:30'} в data/backups/, хранятся последние ${b?.keep ?? 14}. Чтобы восстановить, остановите панель и замените data/panel.db нужным файлом.`}
      actions={
        <Button variant="primary" onClick={() => make.mutate()} disabled={make.isPending}>
          {make.isPending ? 'Копирую…' : 'Сделать копию'}
        </Button>
      }
      flush
    >
      {make.data && (
        <Notice tone="good" role="status" className="mx-4 mb-3 sm:mx-5">
          Создана {make.data.name} ({Math.round(make.data.size_bytes / 1024).toLocaleString('ru-RU')} КБ).
        </Notice>
      )}
      {make.error && <ErrorNotice className="mx-4 mb-3 sm:mx-5" error={make.error} title="Копия не создана" />}
      {backups.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={backups.error} title="Список копий не загрузился" onRetry={() => void backups.refetch()} />}
      {b && b.items.length === 0 && <EmptyState inset="panel" title="Копий пока нет" />}
      {b && b.items.length > 0 && (
        <Table label="Резервные копии">
          <thead>
            <tr>
              <Th>Файл</Th>
              <Th align="right">Размер</Th>
              <Th align="right">Создан</Th>
            </tr>
          </thead>
          <tbody>
            {b.items.map((it) => (
              <tr key={it.name}>
                <Td className="font-mono text-xs">{it.name}</Td>
                <Td align="right">{Math.round(it.size_bytes / 1024).toLocaleString('ru-RU')} КБ</Td>
                <Td align="right" className="whitespace-nowrap text-ink-2">
                  {formatDateTime(it.created_at)}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  )
}

function ApplySection() {
  const status = useQuery({ queryKey: ['apply-status'], queryFn: () => apiGet<ApplyStatus>('/api/applications/status') })
  const s = status.data
  const l = s?.limits
  return (
    <Panel title="Отклики" meta="Лимиты и режим задаются в .env (APPLY_*). Панель не отправляет отклики без вашего согласия и подтверждения пакета; автопилот по умолчанию выключен.">
      {status.error && <ErrorNotice error={status.error} title="Настройки откликов не загрузились" onRetry={() => void status.refetch()} />}
      {status.isLoading && <Skeleton className="h-28 w-full" />}
      {s && l && (
        <Dl
          rows={[
            ['Согласие на отправку', s.consent_accepted_at ? `принято ${formatDateTime(s.consent_accepted_at)}` : 'ещё не принято: запросится перед первым откликом'],
            ['Резюме для откликов', s.resume_id ?? 'не выбрано (основное резюме из профиля или resume_id вручную)'],
            ['Лимиты панели', `${l.max_per_day} в день · ${l.max_per_hour} в час · окно ${l.window} · лимит hh.ru ${l.hh_cap} в сутки`],
            ['Сегодня отправлено', `${l.today_sent} (осталось ${l.remaining_day}), за час ${l.hour_sent}`],
            ['Сухой прогон', s.dry_run ? 'включён: запросы на hh.ru не отправляются' : 'выключен'],
            ['Автопилот', s.autopilot ? `включён для оценки от ${s.autopilot_min_score}` : 'выключен'],
            ...(l.paused_until
              ? ([['Пауза откликов', <span className="text-warn">до {formatDateTime(l.paused_until)}{l.paused_reason ? ` — ${l.paused_reason}` : ''}</span>]] as [string, ReactNode][])
              : []),
          ]}
        />
      )}
    </Panel>
  )
}

function ProbeSection({ loggedIn, settings, report, onDone }: { loggedIn: boolean; settings: SettingsView | undefined; report: ProbeReport | null; onDone: () => void }) {
  const qc = useQueryClient()
  const [allowPublish, setAllowPublish] = useState(false)
  const [allowBlacklist, setAllowBlacklist] = useState(false)
  const [resumeId, setResumeId] = useState('')

  const run = useMutation({
    mutationFn: () =>
      apiPost<ProbeReport>('/api/probe/run', {
        allow_publish: allowPublish,
        allow_blacklist: allowBlacklist,
        resume_id: resumeId.trim() || null,
      }),
    onSuccess: onDone,
  })

  const setMode = useMutation({
    mutationFn: (value: 'auto' | 'api' | 'assist') => apiPut<SettingsView>('/api/settings', { apply_mode_override: value }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['settings'] }),
  })

  const shown = run.data ?? report
  const caps = settings?.capabilities

  return (
    <Panel
      title="Диагностика API hh.ru (зонд)"
      meta={
        <>
          Проверяет с вашим токеном, какие методы доступны приложению, и выбирает режим отклика. Отклик через API проверяется безопасно, на несуществующей вакансии. Занимает около 15 секунд.
          {settings?.probe_at && <div>Последний зонд: {formatDateTime(settings.probe_at)}</div>}
        </>
      }
      actions={
        <Button variant="primary" onClick={() => run.mutate()} disabled={!loggedIn || run.isPending} title={!loggedIn ? 'Сначала войдите в hh.ru' : undefined}>
          {run.isPending ? 'Выполняется…' : 'Запустить зонд'}
        </Button>
      }
      flush
    >
      <div className="space-y-4 px-4 pb-4 sm:px-5">
        {!loggedIn && <p className="text-caption text-ink-3">Сначала войдите в hh.ru.</p>}
        <div className="grid gap-4 md:grid-cols-2">
          <Field
            label="resume_id вручную"
            hint={
              settings?.resumes_hint && settings.resumes_hint.length > 0
                ? `Если список резюме недоступен. Найдены резюме: ${settings.resumes_hint.map((r) => `${r.title || r.id} (${r.id})`).join('; ')}`
                : 'Если список резюме недоступен.'
            }
          >
            <Input placeholder={settings?.resume_id ?? 'например, из https://hh.ru/resume/<id>'} value={resumeId} onChange={(e) => setResumeId(e.target.value)} />
          </Field>
          <div className="space-y-2 pt-1 md:pt-6">
            <Checkbox label="Разрешить проверку «поднять резюме» (реально обновит дату резюме)" checked={allowPublish} onChange={(e) => setAllowPublish(e.target.checked)} />
            <Checkbox label="Разрешить проверку скрытия вакансии (скрыть и сразу вернуть)" checked={allowBlacklist} onChange={(e) => setAllowBlacklist(e.target.checked)} />
          </div>
        </div>
        {run.error && <ErrorNotice error={run.error} title="Зонд не выполнен" />}
        {settings && (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg bg-sunken px-4 py-3 text-sm">
            <span>
              Режим отклика: <span className="font-medium">{settings.apply_mode === 'api' ? 'API' : 'Ассистент'}</span>
            </span>
            <span className="inline-flex items-center gap-1.5 text-ink-2">
              отклик через API: <CapabilityBadge value={caps?.negotiations_post} />
            </span>
            <label className="flex items-center gap-2 sm:ml-auto">
              <span className="text-ink-2">Переопределить</span>
              <Select
                wrapperClassName="w-auto"
                className="bg-surface"
                value={settings.apply_mode_override}
                onChange={(e) => setMode.mutate(e.target.value as 'auto' | 'api' | 'assist')}
                disabled={setMode.isPending}
              >
                <option value="auto">Авто (по зонду)</option>
                <option value="api" disabled={caps?.negotiations_post !== 'available'}>
                  API
                </option>
                <option value="assist">Ассистент</option>
              </Select>
            </label>
          </div>
        )}
        {setMode.error && <ErrorNotice error={setMode.error} title="Режим не переключён" />}
      </div>
      {shown ? <ProbeTable steps={shown.steps} /> : <EmptyState inset="panel" title="Зонд ещё не запускался" />}
    </Panel>
  )
}
