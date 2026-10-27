import { AlertTriangle, CircleCheck, Info, OctagonAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import { errorMessage } from '../../api/client'
import { Button } from './Button'
import { cx } from './cx'

const EMPTY_INSET = {
  /** inside a list frame: room on every side */
  frame: 'px-5 py-10 sm:px-6',
  /** inside a flush panel, under its title: the panel's side padding, little above */
  panel: 'px-4 pb-8 pt-1 sm:px-5',
  /** inside padded content (a tab of a sheet): no padding of its own */
  inline: 'py-2',
}

/** Empty list or block: what is empty, why, and the one action that fixes it. */
export function EmptyState({
  title,
  children,
  action,
  className,
  inset = 'frame',
}: {
  title: string
  children?: ReactNode
  action?: ReactNode
  className?: string
  inset?: keyof typeof EMPTY_INSET
}) {
  return (
    <div className={cx('flex flex-col items-start gap-2', EMPTY_INSET[inset], className)}>
      <div className="text-sm font-semibold text-ink">{title}</div>
      {children && <div className="max-w-xl text-caption text-ink-2">{children}</div>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}

/** Placeholder of a value while it loads: a flat grey bar of the value's size, no shimmer. */
export function Skeleton({ className }: { className?: string }) {
  return <span aria-hidden className={cx('block rounded bg-sunken', className)} />
}

export type NoticeTone = 'info' | 'good' | 'warn' | 'bad'
const NOTICE: Record<NoticeTone, { box: string; icon: typeof Info }> = {
  info: { box: 'border-accent/20 bg-accent-soft text-ink', icon: Info },
  good: { box: 'border-good/20 bg-good-soft text-ink', icon: CircleCheck },
  warn: { box: 'border-warn/25 bg-warn-soft text-ink', icon: AlertTriangle },
  bad: { box: 'border-bad/20 bg-bad-soft text-ink', icon: OctagonAlert },
}
const ICON_TONE: Record<NoticeTone, string> = { info: 'text-accent', good: 'text-good', warn: 'text-warn', bad: 'text-bad' }

/** A message inside the page (state of a job, a result, a warning), never a floating toast. */
export function Notice({ tone = 'info', title, children, action, className, role }: { tone?: NoticeTone; title?: ReactNode; children?: ReactNode; action?: ReactNode; className?: string; role?: 'alert' | 'status' }) {
  const { box, icon: Icon } = NOTICE[tone]
  return (
    <div role={role} className={cx('flex items-start gap-2.5 rounded-lg border px-3.5 py-2.5 text-caption', box, className)}>
      <Icon aria-hidden className={cx('mt-px size-4 shrink-0', ICON_TONE[tone])} />
      <div className="min-w-0 flex-1">
        {title && <div className="font-medium text-ink">{title}</div>}
        {children && <div className={cx('text-ink-2', title ? 'mt-0.5' : null)}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}

/** A failed request inside the block that needed it: what failed, the HTTP code, and «Повторить». */
export function ErrorNotice({ error, title = 'Не удалось загрузить данные', onRetry, className }: { error: unknown; title?: string; onRetry?: () => void; className?: string }) {
  if (!error) return null
  const status = (error as { status?: number })?.status
  return (
    <Notice
      tone="bad"
      role="alert"
      className={className}
      title={`${title}${status ? ` (HTTP ${status})` : ''}`}
      action={
        onRetry && (
          <Button size="sm" onClick={onRetry}>
            Повторить
          </Button>
        )
      }
    >
      {errorMessage(error)}
    </Notice>
  )
}
