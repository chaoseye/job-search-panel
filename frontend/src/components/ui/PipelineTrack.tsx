import { Check } from 'lucide-react'
import { Badge, type BadgeTone } from './Badge'
import { cx } from './cx'

export type StepState = 'done' | 'current' | 'todo' | 'attention'

export interface TrackStep {
  key: string
  label: string
  state: StepState
}

/** Where an object is on its way (found → selected → evaluated → letter → response): done steps are ticked, the
 *  current one is marked, an off-ramp (skipped, hidden, below the threshold) ends the track with its own label.
 *  The connector trails its step, so a track that wraps ends a line with a dash and starts the next with a step. */
export function PipelineTrack({ steps, exit }: { steps: TrackStep[]; exit?: { label: string; tone: BadgeTone } | null }) {
  return (
    <ol className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs" aria-label="Этапы">
      {steps.map((s, i) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span
            className={cx(
              'inline-flex items-center gap-1 whitespace-nowrap',
              s.state === 'done' && 'text-ink-2',
              s.state === 'current' && 'font-medium text-ink',
              s.state === 'attention' && 'font-medium text-warn',
              s.state === 'todo' && 'text-ink-3',
            )}
            aria-current={s.state === 'current' || s.state === 'attention' ? 'step' : undefined}
          >
            {s.state === 'done' ? (
              <Check aria-hidden className="size-3.5" />
            ) : (
              <span
                aria-hidden
                className={cx(
                  'size-2 rounded-full',
                  s.state === 'current' && 'bg-accent ring-3 ring-accent-soft',
                  s.state === 'attention' && 'bg-warn ring-3 ring-warn-soft',
                  s.state === 'todo' && 'border border-line-strong',
                )}
              />
            )}
            {s.label}
          </span>
          {(i < steps.length - 1 || exit) && <span aria-hidden className={cx('h-px w-4', (steps[i + 1]?.state ?? 'done') === 'todo' ? 'bg-line' : 'bg-ink-3/60')} />}
        </li>
      ))}
      {exit && (
        <li className="flex items-center">
          <Badge tone={exit.tone}>{exit.label}</Badge>
        </li>
      )}
    </ol>
  )
}
