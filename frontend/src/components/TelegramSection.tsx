import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Send } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { apiGet, apiPost, apiPut } from '../api/client'
import type { NotificationRow, TelegramStatus } from '../api/types'
import { formatDateTime, shortAgo } from '../lib/format'
import TagInput from './TagInput'
import { Badge, type BadgeTone } from './ui/Badge'
import { Button } from './ui/Button'
import { Checkbox, Input } from './ui/Field'
import { Dl } from './ui/Dl'
import { Panel } from './ui/Panel'
import { ErrorNotice, Notice, Skeleton } from './ui/States'

const SEND_TONE: Record<string, BadgeTone> = { sent: 'good', failed: 'bad' }

function Sub({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2 border-t border-line pt-5">
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      {children}
    </section>
  )
}

/** Telegram notifications block of the Settings page. */
export default function TelegramSection() {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: ['telegram-status'], queryFn: () => apiGet<TelegramStatus>('/api/notifications/telegram'), refetchInterval: 30000 })
  const queue = useQuery({
    queryKey: ['notifications-telegram'],
    queryFn: () => apiGet<NotificationRow[]>('/api/notifications?channel=telegram&limit=10'),
    refetchInterval: 30000,
  })
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['telegram-status'] })
    void qc.invalidateQueries({ queryKey: ['notifications-telegram'] })
  }
  const detect = useMutation({ mutationFn: () => apiPost<{ chat_id: string; chat_title: string | null }>('/api/notifications/telegram/detect-chat'), onSuccess: invalidate })
  const test = useMutation({ mutationFn: () => apiPost<{ ok: boolean; bot: string | null }>('/api/notifications/telegram/test'), onSuccess: invalidate })
  const flush = useMutation({ mutationFn: () => apiPost<{ sent: number; failed: number; deferred: number }>('/api/notifications/telegram/flush'), onSuccess: invalidate })
  const bump = useMutation({
    mutationFn: () => apiPost<{ ok?: boolean; telegram?: boolean; skipped?: string; next?: string | null }>('/api/notifications/resume-bump'),
    onSuccess: invalidate,
  })
  const hot = useMutation({
    mutationFn: () => apiPost<{ ok?: boolean; telegram?: boolean; items?: number; skipped?: string; hot_total?: number }>('/api/analytics/hot-digest'),
    onSuccess: invalidate,
  })
  const s = status.data
  const error = detect.error ?? test.error ?? flush.error ?? bump.error ?? hot.error

  return (
    <Panel
      title="Telegram-уведомления"
      meta={
        s && (
          <>
            {s.bot_username ? (
              <>
                Бот{' '}
                <a className="inline-flex items-center gap-0.5 font-medium text-accent underline-offset-2 hover:underline" href={s.bot_link ?? undefined} target="_blank" rel="noreferrer">
                  @{s.bot_username}
                  <ExternalLink aria-hidden className="size-3" />
                </a>
                {s.bot_name ? ` (${s.bot_name})` : ''}.
              </>
            ) : s.token_set ? (
              'Имя бота не получено от Telegram: проверьте токен и доступ к api.telegram.org.'
            ) : (
              'Создайте бота у @BotFather и вставьте его токен.'
            )}{' '}
            Токен задаётся только в .env (TELEGRAM_BOT_TOKEN) и никуда не отправляется. Чтобы панель узнала ваш чат, напишите боту /start и нажмите «Определить chat_id».
          </>
        )
      }
      actions={
        s && (
          <>
            <Button variant="ghost" onClick={() => detect.mutate()} disabled={!s.token_set || detect.isPending}>
              Определить chat_id
            </Button>
            <Button onClick={() => flush.mutate()} disabled={!s.configured || flush.isPending || !(s.queue.queued > 0)}>
              Отправить очередь
            </Button>
            <Button variant="primary" onClick={() => test.mutate()} disabled={!s.token_set || test.isPending}>
              <Send aria-hidden />
              Тестовое сообщение
            </Button>
          </>
        )
      }
    >
      {status.error && <ErrorNotice error={status.error} title="Состояние Telegram не загрузилось" onRetry={() => void status.refetch()} />}
      {status.isLoading && <Skeleton className="h-32 w-full" />}
      {s && (
        <div className="space-y-5">
          {!s.token_set && <Notice tone="warn">TELEGRAM_BOT_TOKEN не задан в .env: уведомления остаются только в ленте панели.</Notice>}
          {detect.data && (
            <Notice tone="good" role="status">
              chat_id {detect.data.chat_id} сохранён{detect.data.chat_title ? ` (${detect.data.chat_title})` : ''}.
            </Notice>
          )}
          {test.data && (
            <Notice tone="good" role="status">
              Отправлено ботом @{test.data.bot ?? '?'}.
            </Notice>
          )}
          {flush.data && (
            <Notice role="status">
              Отправлено {flush.data.sent}, отложено {flush.data.deferred}, ошибок {flush.data.failed}.
            </Notice>
          )}
          {bump.data && (
            <Notice role="status">
              {bump.data.ok
                ? `Напоминание записано в ленту${bump.data.telegram ? ' и отправлено в Telegram' : ' (в Telegram не уходит: нет токена или событие выключено)'}${bump.data.next ? `; следующее по расписанию — ${bump.data.next}` : ''}.`
                : `Пропущено: ${bump.data.skipped ?? ''}`}
            </Notice>
          )}
          {hot.data && (
            <Notice role="status">
              {hot.data.ok
                ? `Дайджест из ${hot.data.items} вакансий записан в ленту${hot.data.telegram ? ' и отправлен в Telegram' : ' (в Telegram не уходит: нет токена или событие выключено)'}.`
                : `Пропущено: ${hot.data.skipped ?? ''}`}
            </Notice>
          )}
          {error && <ErrorNotice error={error} title="Действие не выполнено" />}

          <Dl
            rows={[
              ['Состояние', s.configured ? <Badge tone="good">настроено</Badge> : <Badge tone="warn">не настроено</Badge>],
              [
                'chat_id',
                <>
                  <span className="tabular-nums">{s.chat_id ?? '—'}</span>
                  {s.chat_id_source === 'settings' && <span className="text-caption text-ink-3"> · определён панелью; можно перенести в .env как TELEGRAM_CHAT_ID</span>}
                  {s.chat_id_source === 'env' && <span className="text-caption text-ink-3"> · из .env</span>}
                </>,
              ],
              ['Вечерняя сводка', `ежедневно в ${s.daily_summary_at} (TELEGRAM_DAILY_SUMMARY_AT)`],
              [
                'Поднять резюме',
                <>
                  {s.resume_bump.label ? (
                    <>
                      напоминание {s.resume_bump.label}
                      {!s.resume_bump.event_enabled && <span className="text-warn"> — событие resume_bump выключено в TELEGRAM_EVENTS</span>}
                    </>
                  ) : (
                    <span className="text-ink-3">напоминание выключено (RESUME_BUMP_REMINDER_AT в .env, например 08:45,13:00,17:15)</span>
                  )}
                  <Button variant="ghost" size="sm" className="ml-1" onClick={() => bump.mutate()} disabled={bump.isPending}>
                    Напомнить сейчас
                  </Button>
                </>,
              ],
              [
                'Горячие вакансии',
                <>
                  {s.hot_digest_at ? (
                    `утренний дайджест ежедневно в ${s.hot_digest_at} (HOT_DIGEST_AT; свежие, с малым числом откликов, повторно не присылаются)`
                  ) : (
                    <span className="text-ink-3">дайджест выключен (HOT_DIGEST_AT в .env, например 09:00); команда боту /hot работает всегда</span>
                  )}
                  <Button variant="ghost" size="sm" className="ml-1" onClick={() => hot.mutate()} disabled={hot.isPending}>
                    Отправить сейчас
                  </Button>
                </>,
              ],
              [
                'Очередь',
                <>
                  <span className="tabular-nums">
                    в очереди {s.queue.queued ?? 0} · отправлено {s.queue.sent ?? 0} · ошибок {s.queue.failed ?? 0} · подавлено {s.queue.suppressed ?? 0}
                  </span>
                  {s.last_sent_at && <span className="text-ink-3"> · последнее {shortAgo(s.last_sent_at)}</span>}
                </>,
              ],
            ]}
          />

          <Sub title="Письма в бота">
            <LettersSwitch config={s.letters} onSaved={invalidate} />
          </Sub>

          <Sub title="Не присылать вакансии">
            <StopWords words={s.stop_words ?? []} onSaved={invalidate} />
          </Sub>

          <Sub title="События">
            <p className="text-caption text-ink-3">Включаются в .env → TELEGRAM_EVENTS; одинаковые события за 5 минут склеиваются в одно сообщение. Зачёркнутые выключены.</p>
            <div className="flex flex-wrap gap-1.5">
              {s.events.map((e) => (
                <Badge key={e.id} tone={e.enabled ? 'good' : 'neutral'} title={e.id} className={e.enabled ? undefined : 'line-through'}>
                  {e.label}
                </Badge>
              ))}
            </div>
          </Sub>

          {queue.data && queue.data.length > 0 && (
            <Sub title="Последние сообщения">
              <ul className="divide-y divide-rule rounded-lg border border-line text-sm">
                {queue.data.map((n) => (
                  <li key={n.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3.5 py-2">
                    <span className="w-14 shrink-0 text-caption tabular-nums text-ink-3" title={formatDateTime(n.created_at)}>
                      {shortAgo(n.created_at)}
                    </span>
                    <Badge tone={SEND_TONE[n.status] ?? 'neutral'}>{n.status}</Badge>
                    <span className="min-w-0 text-ink">{n.label}</span>
                    {n.error && <span className="text-caption text-bad">{n.error}</span>}
                  </li>
                ))}
              </ul>
            </Sub>
          )}
        </div>
      )}
    </Panel>
  )
}

/** Words that keep a vacancy out of the bot: its message is suppressed, the vacancy stays in the panel and the feed. */
function StopWords({ words, onSaved }: { words: string[]; onSaved: () => void }) {
  const [draft, setDraft] = useState<string[]>(words)
  const changed = draft.join('\n') !== words.join('\n')
  const save = useMutation({
    mutationFn: (next: string[]) => apiPut<{ stop_words: string[] }>('/api/notifications/telegram/stop-words', { words: next }),
    onSuccess: (data) => {
      setDraft(data.stop_words)
      onSaved()
    },
  })
  return (
    <div className="space-y-2">
      <p className="text-caption text-ink-3">Вакансии с этими словами в названии не приходят в бота, но остаются в панели. Слово ищется как начало слова: «продаж» найдёт «продажи», «продажам».</p>
      <TagInput label="Слова-исключения" value={draft} onChange={setDraft} placeholder="продаж, курьер, кассир…" />
      <div className="flex flex-wrap items-center gap-2">
        <Button variant={changed ? 'primary' : 'secondary'} onClick={() => save.mutate(draft)} disabled={!changed || save.isPending}>
          Сохранить слова
        </Button>
        {save.error && <ErrorNotice className="w-full" error={save.error} title="Слова не сохранены" />}
      </div>
    </div>
  )
}

/** The switch «vacancy with its cover letter → the bot» (event letter) and its score threshold. */
function LettersSwitch({ config, onSaved }: { config: TelegramStatus['letters']; onSaved: () => void }) {
  const [minScore, setMinScore] = useState(String(config.min_score))
  const save = useMutation({
    mutationFn: (enabled: boolean) => apiPut<TelegramStatus['letters']>('/api/notifications/telegram/letters', { enabled, min_score: Number(minScore) || 0 }),
    onSuccess: onSaved,
  })
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Checkbox label="Присылать каждую подходящую вакансию с письмом" checked={config.enabled} disabled={save.isPending} onChange={(e) => save.mutate(e.target.checked)} />
        <label className="flex items-center gap-2 text-sm text-ink-2">
          оценка от
          <Input
            className="w-16"
            inputMode="numeric"
            value={minScore}
            onChange={(e) => setMinScore(e.target.value.replace(/\D/g, '').slice(0, 3))}
            onBlur={() => {
              if (Number(minScore) !== config.min_score) save.mutate(config.enabled)
            }}
          />
        </label>
      </div>
      <p className="text-caption text-ink-3">Кнопки в сообщении («Да, отправлено», «Пропустить», «Отложить») делают то же, что в панели. Обработанная в панели вакансия исчезает из чата.</p>
      {save.error && <ErrorNotice error={save.error} title="Настройка не сохранена" />}
    </div>
  )
}
