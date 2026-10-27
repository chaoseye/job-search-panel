import { cx } from './cx'

/** Score bands of the panel: from 70 a vacancy suits, from 50 it is a maybe (SCORE_THRESHOLD_* defaults). */
export const SUITABLE_FROM = 70
export const MAYBE_FROM = 50

export type ScoreTone = 'good' | 'warn' | 'low'

export function scoreTone(score: number): ScoreTone {
  return score >= SUITABLE_FROM ? 'good' : score >= MAYBE_FROM ? 'warn' : 'low'
}

const CHIP: Record<ScoreTone, string> = {
  good: 'bg-good-soft text-good',
  warn: 'bg-warn-soft text-warn',
  low: 'bg-sunken text-ink-2',
}
const FILL: Record<ScoreTone, string> = { good: 'bg-good', warn: 'bg-warn', low: 'bg-ink-3' }

/** Score in a list row: Claude's score on its band colour; the panel's preliminary score outlined and dashed. */
export function ScoreChip({ score, prescore, className }: { score: number | null; prescore: number | null; className?: string }) {
  const box = 'inline-flex h-6 min-w-9 items-center justify-center rounded-md px-1.5 text-sm font-semibold tabular-nums'
  if (score !== null)
    return (
      <span className={cx(box, CHIP[scoreTone(score)], className)} title={`Оценка Claude: ${score} из 100`}>
        {score}
      </span>
    )
  if (prescore !== null)
    return (
      <span className={cx(box, 'border border-dashed border-line-strong font-medium text-ink-3', className)} title={`Предварительная оценка панели: ${prescore} (Claude ещё не разбирал)`}>
        {prescore}
      </span>
    )
  return (
    <span className={cx(box, 'text-ink-3', className)} title="Оценки нет">
      —
    </span>
  )
}

/** Score in a card: the number, what it means, and where it sits against the two thresholds. */
export function ScoreMeter({ score, prescore, verdict, note }: { score: number | null; prescore: number | null; verdict?: string | null; note?: string | null }) {
  const value = score ?? prescore
  if (value === null) return <div className="text-sm text-ink-3">Оценки пока нет</div>
  const tone = scoreTone(value)
  const preliminary = score === null
  return (
    <div className="flex items-center gap-3">
      <span
        className={cx(
          'inline-flex h-9 min-w-12 items-center justify-center rounded-lg px-2 text-lg font-semibold tabular-nums',
          preliminary ? 'border border-dashed border-line-strong text-ink-2' : CHIP[tone],
        )}
      >
        {value}
      </span>
      <div className="min-w-0">
        <div className="text-sm font-medium text-ink">{preliminary ? 'Предварительно' : (verdict ?? 'Оценка Claude')}</div>
        <div className="mt-1 flex items-center gap-2">
          <span className="relative block h-1.5 w-36 rounded-full bg-sunken" aria-hidden>
            <span className={cx('absolute inset-y-0 left-0 rounded-full', preliminary ? 'bg-line-strong' : FILL[tone])} style={{ width: `${Math.max(2, Math.min(100, value))}%` }} />
            <span className="absolute -top-0.5 h-2.5 w-px bg-ink-3/50" style={{ left: `${MAYBE_FROM}%` }} />
            <span className="absolute -top-0.5 h-2.5 w-px bg-ink-3/50" style={{ left: `${SUITABLE_FROM}%` }} />
          </span>
          {note && <span className="truncate text-xs text-ink-3">{note}</span>}
        </div>
      </div>
    </div>
  )
}
