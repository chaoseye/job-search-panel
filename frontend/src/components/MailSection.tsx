import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { apiGet, apiPost } from '../api/client'
import type { MailStatus } from '../api/types'
import { formatDateTime, relativeTime, shortAgo } from '../lib/format'
import { Badge } from './ui/Badge'
import { Button } from './ui/Button'
import { cx } from './ui/cx'
import { Disclosure } from './ui/Disclosure'
import { Dl } from './ui/Dl'
import { Panel } from './ui/Panel'
import { ErrorNotice, Notice, Skeleton } from './ui/States'

/** hh.ru notification e-mails → manual journal (Settings page block). */
export default function MailSection() {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: ['mail-status'], queryFn: () => apiGet<MailStatus>('/api/mail/status'), refetchInterval: 60000 })
  const sync = useMutation({
    mutationFn: () => apiPost<MailStatus>('/api/mail/sync'),
    onSuccess: (data) => {
      qc.setQueryData(['mail-status'], data)
      void qc.invalidateQueries({ queryKey: ['applications'] })
      void qc.invalidateQueries({ queryKey: ['stats'] })
    },
  })
  const s = status.data
  const last = s?.last ?? {}
  const summary = last.summary
  return (
    <Panel
      title="Письма hh.ru → журнал ответов"
      meta="Панель читает уведомления hh.ru из почты и записывает просмотры, приглашения, отказы и сообщения в журнал откликов."
      actions={
        s && (
          <Button onClick={() => sync.mutate()} disabled={!s.configured || sync.isPending}>
            <RefreshCw aria-hidden className={cx(sync.isPending && 'animate-spin motion-reduce:animate-none')} />
            {sync.isPending ? 'Читаю почту…' : 'Синхронизировать'}
          </Button>
        )
      }
    >
      {status.error && <ErrorNotice error={status.error} title="Состояние почты не загрузилось" onRetry={() => void status.refetch()} />}
      {status.isLoading && <Skeleton className="h-20 w-full" />}
      {s && (
        <div className="space-y-4">
          <Disclosure summary="Как это работает">
            <ul className="list-disc space-y-1 pl-5 text-caption text-ink-2">
              <li>hh.ru закрыл переписку для приложения, но письма «работодатель просмотрел отклик», «приглашение», «отказ», «новое сообщение» приходят на почту.</li>
              <li>
                Панель читает их по IMAP, только чтение, письма не трогает. Отклик находится по ссылке на вакансию или по её названию; воронка, напоминания и черновики ответов работают как
                при синхронизации.
              </li>
              <li>Настройка в .env: IMAP_HOST, IMAP_USER, IMAP_PASSWORD (для Gmail нужен пароль приложения при включённой двухэтапной проверке), IMAP_SYNC_MIN.</li>
              <li>По приглашению панель сразу заказывает у Claude подготовку к собеседованию, по сообщению работодателя заказывает черновик ответа. Оба уходят в Telegram как обычные события.</li>
            </ul>
          </Disclosure>
          {!s.configured && <Notice tone="warn">IMAP не настроен: журнал заполняется вручную, в карточке отклика на странице «Отклики».</Notice>}
          <Dl
            rows={[
              [
                'Ящик',
                <>
                  {s.configured ? `${s.user} на ${s.host}, папка ${s.folder}, от «${s.sender_filter}»` : '—'}
                  {s.configured && s.interval_min > 0 && <span className="text-ink-3"> · каждые {s.interval_min} мин</span>}
                </>,
              ],
              [
                'Последняя синхронизация',
                last.at ? (
                  <>
                    <span title={formatDateTime(last.at)}>{relativeTime(last.at)}</span>
                    {last.error ? (
                      <span className="text-bad"> — ошибка: {last.error}</span>
                    ) : summary ? (
                      <span className="text-ink-3">
                        {' '}
                        · писем {summary.fetched}, записано {summary.matched}, не сопоставлено {summary.unmatched}, не про отклики {summary.ignored}
                      </span>
                    ) : null}
                  </>
                ) : (
                  '—'
                ),
              ],
            ]}
          />
          {sync.error && <ErrorNotice error={sync.error} title="Почта не прочитана" />}
          {s.unmatched.length > 0 && (
            <div>
              <h3 className="mb-2 text-caption font-medium text-ink-2">Письма про отклики, которые не удалось сопоставить: запишите их вручную в «Откликах»</h3>
              <ul className="divide-y divide-rule rounded-lg border border-line text-sm">
                {s.unmatched.map((u, i) => (
                  <li key={i} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3.5 py-2">
                    <span className="w-14 shrink-0 text-caption tabular-nums text-ink-3">{u.at ? shortAgo(u.at) : ''}</span>
                    <Badge>{u.kind}</Badge>
                    <span className="min-w-0 text-ink">{u.subject}</span>
                    {u.error && <span className="text-caption text-bad">{u.error}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}
