import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pause, Play } from 'lucide-react'
import { useState } from 'react'
import { apiGet, apiPost } from '../api/client'
import type { AuditRow, HealthView, JobsView } from '../api/types'
import { Badge, type BadgeTone } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Dl } from '../components/ui/Dl'
import { Select } from '../components/ui/Field'
import { PageHeader } from '../components/ui/PageHeader'
import { Panel } from '../components/ui/Panel'
import { Segmented } from '../components/ui/Segmented'
import { EmptyState, ErrorNotice, Skeleton } from '../components/ui/States'
import { Table, Td, Th } from '../components/ui/Table'
import { formatDateTime, shortAgo } from '../lib/format'

/** «Запустить сейчас» (F-8.12.1): scan / batch / import / apply / sync. */
const RUN_NOW: { id: string; label: string; path: string }[] = [
  { id: 'scan', label: 'Сканировать', path: '/api/jobs/scan' },
  { id: 'batch', label: 'Собрать пакет', path: '/api/batches/create' },
  { id: 'import', label: 'Импортировать результаты', path: '/api/batches/import' },
  { id: 'apply', label: 'Отправить очередь', path: '/api/applications/run' },
  { id: 'sync', label: 'Синхронизировать отклики', path: '/api/jobs/sync' },
  { id: 'freshness', label: 'Проверить закрытые', path: '/api/jobs/freshness' },
]

const LEVEL_TONE: Record<string, BadgeTone> = { error: 'bad', warning: 'warn', info: 'neutral' }
const RUN_TONE: Record<string, BadgeTone> = { error: 'bad', running: 'info', ok: 'good' }
const LEVELS: [string, string][] = [
  ['', 'Все'],
  ['info', 'info'],
  ['warning', 'warning'],
  ['error', 'error'],
]

export default function LogsPage() {
  const qc = useQueryClient()
  const health = useQuery({ queryKey: ['health'], queryFn: () => apiGet<HealthView>('/api/health'), refetchInterval: 15000 })
  const jobs = useQuery({ queryKey: ['jobs'], queryFn: () => apiGet<JobsView>('/api/jobs?limit=30'), refetchInterval: 10000 })
  const [area, setArea] = useState('')
  const [level, setLevel] = useState('')
  const audit = useQuery({ queryKey: ['audit'], queryFn: () => apiGet<AuditRow[]>('/api/audit?limit=300'), refetchInterval: 15000 })
  const run = useMutation({
    mutationFn: (path: string) => apiPost(path, {}),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['jobs'] })
      void qc.invalidateQueries({ queryKey: ['audit'] })
    },
  })
  const pause = useMutation({
    mutationFn: (resume: boolean) => apiPost(resume ? '/api/jobs/resume' : '/api/jobs/pause', resume ? {} : { reason: 'user' }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['health'] })
      void qc.invalidateQueries({ queryKey: ['jobs'] })
      void qc.invalidateQueries({ queryKey: ['settings'] })
      void qc.invalidateQueries({ queryKey: ['audit'] })
    },
  })
  const areas = Array.from(new Set((audit.data ?? []).map((r) => r.area))).sort()
  const rows = (audit.data ?? []).filter((r) => (!area || r.area === area) && (!level || r.level === level))
  const h = health.data
  const paused = Boolean(h?.paused_reason)
  const failures = jobs.data ? Object.entries(jobs.data.failures).filter(([, n]) => n > 0) : []

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Журнал"
        meta={h && `Версия ${h.version} · режим ${h.apply_mode}`}
        actions={
          h && (
            <Button variant={paused ? 'primary' : 'secondary'} onClick={() => pause.mutate(paused)} disabled={pause.isPending}>
              {paused ? <Play aria-hidden /> : <Pause aria-hidden />}
              {paused ? 'Снять паузу' : 'Поставить на паузу'}
            </Button>
          )
        }
      />
      {pause.error && <ErrorNotice error={pause.error} title="Пауза не переключилась" />}

      <Panel title="Состояние">
        {health.error && <ErrorNotice error={health.error} title="Состояние не загрузилось" onRetry={() => void health.refetch()} />}
        {health.isLoading && <Skeleton className="h-24 w-full" />}
        {h && (
          <Dl
            rows={[
              ['Пауза', paused ? <Badge tone="bad">{h.paused_reason === 'user' ? 'включена вручную' : h.paused_reason}</Badge> : 'нет'],
              ['Сейчас выполняется', jobs.data?.running.join(', ') || 'ничего'],
              ['Последний скан', jobs.data?.last_scan_at ? <span title={formatDateTime(jobs.data.last_scan_at)}>{shortAgo(jobs.data.last_scan_at)}</span> : '—'],
              ['Последняя синхронизация', jobs.data?.last_sync_at ? <span title={formatDateTime(jobs.data.last_sync_at)}>{shortAgo(jobs.data.last_sync_at)}</span> : '—'],
              ['Ошибки подряд', failures.length ? <span className="text-bad">{failures.map(([j, n]) => `${j}: ${n}`).join(', ')}</span> : 'нет'],
              ['Планировщик', <span className="text-caption text-ink-2">{h.scheduler_jobs.join(', ') || '—'}</span>],
            ]}
          />
        )}
        <div className="mt-5 border-t border-line pt-4">
          <h3 className="mb-2 text-caption font-medium text-ink-2">Запустить сейчас</h3>
          <div className="flex flex-wrap gap-2">
            {RUN_NOW.map((j) => (
              <Button key={j.id} size="sm" onClick={() => run.mutate(j.path)} disabled={run.isPending || jobs.data?.running.includes(j.id)}>
                {j.label}
              </Button>
            ))}
          </div>
          {run.error && <ErrorNotice className="mt-3" error={run.error} title="Задача не запустилась" />}
        </div>
      </Panel>

      <Panel title="Фоновые задачи" flush>
        {jobs.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={jobs.error} title="Задачи не загрузились" onRetry={() => void jobs.refetch()} />}
        {jobs.data && jobs.data.runs.length === 0 && <EmptyState inset="panel" title="Задачи ещё не запускались" />}
        {jobs.data && jobs.data.runs.length > 0 && (
          <Table label="Фоновые задачи" minWidth={720}>
            <thead>
              <tr>
                <Th>Задача</Th>
                <Th>Статус</Th>
                <Th>Начало</Th>
                <Th align="right">Длительность</Th>
                <Th>Итог</Th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.runs.map((r) => (
                <tr key={r.id}>
                  <Td className="whitespace-nowrap font-medium">{r.job}</Td>
                  <Td>
                    <Badge tone={RUN_TONE[r.status] ?? 'neutral'}>{r.status}</Badge>
                  </Td>
                  <Td className="whitespace-nowrap tabular-nums text-ink-2">{formatDateTime(r.started_at)}</Td>
                  <Td align="right" className="whitespace-nowrap text-ink-2">
                    {r.duration_sec !== null ? `${r.duration_sec} с` : '—'}
                  </Td>
                  <Td className={r.error ? 'text-caption text-bad' : 'text-caption text-ink-2'}>
                    <span className="line-clamp-3 break-all">{r.error ?? (r.stats ? JSON.stringify(r.stats).slice(0, 200) : '')}</span>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>

      <Panel
        title="События"
        flush
        actions={
          <>
            <Select aria-label="Область" wrapperClassName="w-auto" value={area} onChange={(e) => setArea(e.target.value)}>
              <option value="">Все области</option>
              {areas.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </Select>
            <Segmented label="Уровень" options={LEVELS} value={level} onChange={setLevel} />
          </>
        }
      >
        {audit.error && <ErrorNotice className="mx-4 mb-4 sm:mx-5" error={audit.error} title="События не загрузились" onRetry={() => void audit.refetch()} />}
        {audit.data && rows.length === 0 && <EmptyState inset="panel" title="Пока пусто" />}
        {rows.length > 0 && (
          <Table label="События" minWidth={720}>
            <thead>
              <tr>
                <Th>Время</Th>
                <Th>Уровень</Th>
                <Th>Область</Th>
                <Th>Сообщение</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <Td className="whitespace-nowrap tabular-nums text-ink-2">{formatDateTime(row.ts)}</Td>
                  <Td>
                    <Badge tone={LEVEL_TONE[row.level] ?? 'neutral'}>{row.level}</Badge>
                  </Td>
                  <Td className="whitespace-nowrap text-ink-2">{row.area}</Td>
                  <Td className="min-w-[20rem]">{row.message}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>
    </div>
  )
}
