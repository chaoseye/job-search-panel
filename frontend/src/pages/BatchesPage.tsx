import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, Play, Plus } from 'lucide-react'
import { useState } from 'react'
import { apiDelete, apiGet, apiPost, apiText, errorMessage } from '../api/client'
import type { BatchesSummary, ClaudeBatch, ClaudeStatus, ImportOutcome } from '../api/types'
import { Badge, type BadgeTone } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Textarea } from '../components/ui/Field'
import { PageHeader } from '../components/ui/PageHeader'
import { Panel } from '../components/ui/Panel'
import { Stat } from '../components/ui/Stat'
import { EmptyState, ErrorNotice, Notice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { formatDateTime, shortAgo } from '../lib/format'

const STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  pending: { label: 'Ждёт разбора', tone: 'info' },
  imported: { label: 'Импортирован', tone: 'good' },
  partial: { label: 'Импортирован частично', tone: 'warn' },
  rejected: { label: 'Отклонён', tone: 'bad' },
  cancelled: { label: 'Отменён', tone: 'neutral' },
  skipped: { label: 'Пропущен', tone: 'neutral' },
}

function Code({ children }: { children: string }) {
  return <code className="rounded bg-sunken px-1.5 py-0.5 font-mono text-[0.85em] text-ink">{children}</code>
}

/** «Разбор»: the packets of vacancies, letters and other tasks that Claude Code handles through the exchange folder. */
export default function BatchesPage() {
  const qc = useQueryClient()
  const list = useQuery({
    queryKey: ['batches'],
    queryFn: () => apiGet<{ items: ClaudeBatch[]; summary: BatchesSummary }>('/api/batches'),
    refetchInterval: 10000,
  })
  const [pasted, setPasted] = useState('')
  const [lastOutcomes, setLastOutcomes] = useState<ImportOutcome[]>([])
  const [showGuidelines, setShowGuidelines] = useState(false)
  const guidelines = useQuery({ queryKey: ['guidelines'], queryFn: () => apiText('/api/batches/guidelines'), enabled: showGuidelines })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['batches'] })
    void qc.invalidateQueries({ queryKey: ['batches-summary'] })
    void qc.invalidateQueries({ queryKey: ['vacancies'] })
    void qc.invalidateQueries({ queryKey: ['vacancy-summary'] })
  }
  const create = useMutation({
    mutationFn: () => apiPost<{ created: ClaudeBatch[] }>('/api/batches/create', {}),
    onSuccess: invalidate,
  })
  const importNow = useMutation({
    mutationFn: () => apiPost<{ outcomes: ImportOutcome[] }>('/api/batches/import'),
    onSuccess: (r) => {
      setLastOutcomes(r.outcomes)
      invalidate()
    },
  })
  const importText = useMutation({
    mutationFn: () => apiPost<{ outcome: ImportOutcome }>('/api/batches/import-text', { text: pasted }),
    onSuccess: (r) => {
      setLastOutcomes([r.outcome])
      setPasted('')
      invalidate()
    },
  })
  const cancel = useMutation({ mutationFn: (id: number) => apiDelete(`/api/batches/${id}`), onSuccess: invalidate })
  const cancelPending = useMutation({
    mutationFn: () => apiPost<{ cancelled: number; returned: number }>('/api/batches/cancel-pending'),
    onSuccess: invalidate,
  })
  const claude = useQuery({ queryKey: ['claude-status'], queryFn: () => apiGet<ClaudeStatus>('/api/claude/status'), refetchInterval: 15000 })
  const runClaude = useMutation({
    mutationFn: () => apiPost('/api/claude/run', {}),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['claude-status'] })
      setTimeout(invalidate, 2000)
    },
  })

  const summary = list.data?.summary
  const items = list.data?.items ?? []
  const c = claude.data
  const error = create.error ?? importNow.error ?? importText.error ?? cancel.error ?? cancelPending.error ?? runClaude.error

  let meta = 'Вакансии, письма и другие задачи для Claude Code: панель складывает их в пакеты, Claude разбирает и возвращает результат.'
  if (c?.enabled && c.executable)
    meta =
      c.tick_min > 0
        ? `Автономный разбор: каждые ${c.tick_min} мин панель сама запускает claude -p "/hh-batch", если есть пакеты. После неудачи ждёт, после трёх неудач подряд останавливается до ручного запуска.`
        : 'Автономный разбор выключен (CLAUDE_CLI_TICK_MIN=0): только по кнопке.'

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Разбор"
        meta={meta}
        actions={
          <>
            <Button variant="ghost" onClick={() => importNow.mutate()} disabled={importNow.isPending}>
              <Download aria-hidden />
              Импортировать сейчас
            </Button>
            {c?.enabled && (
              <Button
                onClick={() => runClaude.mutate()}
                disabled={runClaude.isPending || c.job_running || !c.available || c.pending.length === 0}
                title={c.executable ? `${c.executable} -p "/hh-batch" в ${c.cwd}` : 'Команда claude не найдена в PATH'}
              >
                <Play aria-hidden />
                {c.job_running ? 'Claude разбирает…' : 'Запустить разбор'}
              </Button>
            )}
            <Button variant="primary" onClick={() => create.mutate()} disabled={create.isPending || !summary?.waiting_for_packet}>
              <Plus aria-hidden />
              Сформировать пакет{summary?.waiting_for_packet ? ` (${summary.waiting_for_packet})` : ''}
            </Button>
          </>
        }
      />

      {c?.enabled && !c.executable && (
        <Notice tone="warn" title="Команда claude не найдена">
          CLAUDE_CLI_AUTORUN включён, но в PATH нет claude: запускайте /hh-batch в Claude Code вручную.
        </Notice>
      )}
      {error && <ErrorNotice error={error} title="Действие не выполнено" />}
      {list.error && <ErrorNotice error={list.error} title="Не удалось загрузить пакеты" onRetry={() => void list.refetch()} />}

      {summary && (
        <section aria-label="Очередь разбора" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label="Ждут пакета" value={summary.waiting_for_packet} sub={`кандидатов ${summary.prescored}, переработок писем ${summary.revision_requests}`} />
          <Stat label="В пакетах у Claude" value={summary.awaiting_claude} sub={`пакетов: ${summary.pending_batches}`} />
          <Stat label="Файлов в pending/" value={summary.pending_files.length} />
          <Stat label="Профиль" value={summary.profile_version ? `v${summary.profile_version}` : '—'} sub={summary.profile_version ? undefined : 'соберите профиль'} />
        </section>
      )}

      {lastOutcomes.length > 0 && (
        <Panel
          title="Результат импорта"
          actions={
            <Button variant="ghost" size="sm" onClick={() => setLastOutcomes([])}>
              Скрыть
            </Button>
          }
        >
          <ul className="space-y-2">
            {lastOutcomes.map((o) => (
              <li key={o.file} className="rounded-lg border border-line px-3.5 py-2.5 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={STATUS[o.status]?.tone ?? 'neutral'}>{STATUS[o.status]?.label ?? o.status}</Badge>
                  <span className="font-mono text-xs text-ink-2">{o.file}</span>
                  <span className="text-ink-2">
                    оценок {o.evaluations}, писем {o.letters}
                  </span>
                  {o.stale_profile && <Badge tone="warn">старая версия профиля</Badge>}
                </div>
                {o.errors.length > 0 && (
                  <ul className="mt-1.5 list-disc pl-5 text-caption text-bad">
                    {o.errors.map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                )}
                {o.warnings.length > 0 && (
                  <ul className="mt-1.5 list-disc pl-5 text-caption text-warn">
                    {o.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                )}
                {o.missing.length > 0 && <div className="mt-1.5 text-caption text-ink-2">Без результата, вернутся в очередь: {o.missing.join(', ')}</div>}
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel
        title="Пакеты"
        flush
        actions={
          summary && summary.pending_batches > 0 ? (
            <Button
              variant="danger"
              size="sm"
              disabled={cancelPending.isPending}
              onClick={() => {
                if (window.confirm(`Отменить все ${summary.pending_batches} ожидающих пакетов? Вакансии вернутся в очередь, файлы из pending/ будут удалены.`)) cancelPending.mutate()
              }}
            >
              Отменить все ожидающие
            </Button>
          ) : undefined
        }
      >
        {list.isLoading && (
          <div aria-busy="true" aria-label="Загрузка пакетов" className="space-y-3 px-5 pb-5">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        )}
        {list.data && items.length === 0 && <EmptyState inset="panel" title="Пакетов ещё нет">Нажмите «Сформировать пакет», когда появятся вакансии для разбора.</EmptyState>}
        {items.length > 0 && (
          <Table label="Пакеты" minWidth={640}>
            <thead>
              <tr>
                <Th>Пакет</Th>
                <Th>Создан</Th>
                <Th align="right">Задач</Th>
                <Th>Статус</Th>
                <Th>Импортирован</Th>
                <Th>
                  <span className="sr-only">Действия</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {items.map((b) => (
                <tr key={b.id}>
                  <Td>
                    <span className="font-mono text-xs text-ink">batch_{b.batch_id}.json</span>
                    {b.profile_version && <div className="text-xs text-ink-3">профиль v{b.profile_version}</div>}
                  </Td>
                  <Td className="whitespace-nowrap tabular-nums text-ink-2" title={formatDateTime(b.created_at)}>
                    {shortAgo(b.created_at)}
                  </Td>
                  <Td align="right">
                    {b.tasks_count}
                    {b.status !== 'pending' && b.status !== 'cancelled' && <div className="text-xs text-ink-3">результатов {b.results_count}</div>}
                  </Td>
                  <Td>
                    <Badge tone={STATUS[b.status]?.tone ?? 'neutral'}>{STATUS[b.status]?.label ?? b.status}</Badge>
                    {b.error && <div className="mt-1 max-w-md text-caption text-bad">{b.error}</div>}
                  </Td>
                  <Td className="whitespace-nowrap tabular-nums text-ink-2">{b.imported_at ? formatDateTime(b.imported_at) : '—'}</Td>
                  <Td className="w-px text-right">
                    {b.status === 'pending' && (
                      <Button
                        variant="danger"
                        size="sm"
                        disabled={cancel.isPending}
                        onClick={() => {
                          if (window.confirm('Отменить пакет? Вакансии вернутся в очередь.')) cancel.mutate(b.id)
                        }}
                      >
                        Отменить
                      </Button>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>

      {summary && (
        <Panel title="Как разобрать пакет">
          <ol className="list-decimal space-y-1.5 pl-5 text-sm text-ink-2">
            <li>
              Откройте Claude Code в папке проекта и выполните команду <Code>{summary.command}</Code>.
              {summary.pending_files.length > 0 && (
                <>
                  {' '}
                  Файлы в очереди: <span className="break-all font-mono text-xs">{summary.pending_files.join(', ')}</span>.
                </>
              )}
            </li>
            <li>
              Claude прочитает profile.md и guidelines.md, разберёт задачи и запишет результат в <Code>done/</Code>.
            </li>
            <li>Панель импортирует результат сама (или нажмите «Импортировать сейчас»). Оценки и письма появятся в карточках вакансий.</li>
          </ol>
          <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-ink-3">
            <span>
              Папка обмена: <span className="break-all font-mono text-xs text-ink-2">{summary.exchange_dir}</span>
            </span>
            <span>· размер пакета {summary.batch_size}</span>
            <span>· автосоздание после сканирования: {summary.auto_create ? 'да' : 'нет'}</span>
            <Button variant="ghost" size="sm" aria-expanded={showGuidelines} onClick={() => setShowGuidelines(!showGuidelines)}>
              {showGuidelines ? 'Скрыть guidelines.md' : 'Показать guidelines.md'}
            </Button>
          </div>
          {showGuidelines && (
            <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-canvas p-4 font-mono text-xs leading-relaxed text-ink">
              {guidelines.isLoading ? 'Загрузка…' : guidelines.error ? errorMessage(guidelines.error) : guidelines.data}
            </pre>
          )}
        </Panel>
      )}

      <Panel title="Вставить результат вручную" meta="Если Claude вывел JSON результата в чат, а не в файл, вставьте его сюда: панель сохранит его в done/ и импортирует.">
        <Textarea
          aria-label="JSON результата"
          className="min-h-36 font-mono text-xs"
          value={pasted}
          onChange={(e) => setPasted(e.target.value)}
          placeholder='{"protocol_version": 1, "batch_id": "...", "results": [...]}'
        />
        <Button className="mt-3" onClick={() => importText.mutate()} disabled={!pasted.trim() || importText.isPending}>
          Импортировать из текста
        </Button>
      </Panel>
    </div>
  )
}
