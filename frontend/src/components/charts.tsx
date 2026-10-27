/** Small dependency-free SVG charts, coloured with the design tokens through utility classes. */
import { useLayoutEffect, useRef, useState } from 'react'
import type { StatsSeriesDay } from '../api/types'
import { cx } from './ui/cx'

/** Outcome of a sent response and its colour: the fill of a bar, the swatch of the legend. */
export const OUTCOMES: { key: string; label: string; fill: string; swatch: string }[] = [
  { key: 'invited', label: 'приглашение', fill: 'fill-good', swatch: 'bg-good' },
  { key: 'viewed', label: 'просмотрен', fill: 'fill-accent', swatch: 'bg-accent' },
  { key: 'discarded', label: 'отказ', fill: 'fill-bad', swatch: 'bg-bad' },
  { key: 'sent', label: 'без реакции', fill: 'fill-line-strong', swatch: 'bg-line-strong' },
]
const OUTCOME = Object.fromEntries(OUTCOMES.map((o) => [o.key, o]))

function shortDate(iso: string): string {
  const [, m, d] = iso.split('-')
  return `${d}.${m}`
}

/** Width of the block a chart sits in, kept current on resize (0 until measured, and where ResizeObserver is absent). */
function useWidth() {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setWidth(el.clientWidth)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => setWidth(Math.floor(entries[0].contentRect.width)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, width] as const
}

const NUM = { fontVariantNumeric: 'tabular-nums' } as const

/** Stacked daily bars (sent by outcome) with optional line series (found / suitable). Fills its block; a long period
 *  on a narrow screen scrolls sideways instead of squeezing the bars. */
export function DailyChart({
  days,
  stacks,
  lines,
  height = 200,
}: {
  days: StatsSeriesDay[]
  stacks: string[]
  lines?: { key: string; className: string; label: string }[]
  height?: number
}) {
  const [ref, box] = useWidth()
  const width = Math.max(days.length * 12 + 40, box || 320)
  const padL = 32
  const padB = 22
  const top = 8
  const innerH = height - padB - top
  const value = (d: StatsSeriesDay, k: string) => Number(d[k as keyof StatsSeriesDay] ?? 0)
  const barMax = Math.max(1, ...days.map((d) => stacks.reduce((a, k) => a + value(d, k), 0)))
  const lineMax = Math.max(1, ...days.map((d) => Math.max(0, ...(lines ?? []).map((l) => value(d, l.key)))))
  const axisMax = stacks.length ? barMax : lineMax
  const slot = (width - padL - 8) / Math.max(1, days.length)
  const bw = Math.max(2, Math.min(slot - 4, 28))
  const labelEvery = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor((width - padL) / 44))))
  return (
    <div ref={ref} className="overflow-x-auto">
      {days.length === 0 ? (
        <p className="text-caption text-ink-3">Нет данных за период.</p>
      ) : (
        <svg width={width} height={height} className="block" role="img" aria-label={`График по дням, ${shortDate(days[0].date)}–${shortDate(days[days.length - 1].date)}`}>
          {[0, 0.5, 1].map((f) => (
            <g key={f}>
              <line x1={padL} x2={width - 4} y1={top + innerH * (1 - f)} y2={top + innerH * (1 - f)} className="stroke-line" strokeDasharray={f ? '2 3' : undefined} />
              <text x={padL - 6} y={top + innerH * (1 - f) + 4} fontSize={11} textAnchor="end" className="fill-ink-3" style={NUM}>
                {Math.round(axisMax * f)}
              </text>
            </g>
          ))}
          {days.map((d, i) => {
            let y = top + innerH
            const x = padL + i * slot + (slot - bw) / 2
            return (
              <g key={d.date}>
                {stacks.map((k) => {
                  const v = value(d, k)
                  const h = (v / barMax) * innerH
                  y -= h
                  return v > 0 ? (
                    <rect key={k} x={x} y={y} width={bw} height={h} className={OUTCOME[k]?.fill ?? 'fill-ink-3'}>
                      <title>{`${shortDate(d.date)} · ${OUTCOME[k]?.label ?? k}: ${v}`}</title>
                    </rect>
                  ) : null
                })}
                {i % labelEvery === 0 && (
                  <text x={x + bw / 2} y={height - 6} fontSize={11} textAnchor="middle" className="fill-ink-3" style={NUM}>
                    {shortDate(d.date)}
                  </text>
                )}
              </g>
            )
          })}
          {(lines ?? []).map((l) => {
            const pts = days.map((d, i) => `${padL + i * slot + slot / 2},${top + innerH - (value(d, l.key) / lineMax) * innerH}`)
            return (
              <polyline key={l.key} points={pts.join(' ')} fill="none" className={l.className} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round">
                <title>{l.label}</title>
              </polyline>
            )
          })}
        </svg>
      )}
    </div>
  )
}

const BAR_TONE = {
  accent: 'bg-accent',
  good: 'bg-good',
  warn: 'bg-warn',
  bad: 'bg-bad',
  neutral: 'bg-ink-3',
} as const

/** Horizontal bars, the longest = the largest value; the label, the bar and the number in three columns. */
export function HBars({
  rows,
  tone = 'accent',
  formatValue,
}: {
  rows: { label: string; value: number; hint?: string }[]
  tone?: keyof typeof BAR_TONE
  formatValue?: (v: number) => string
}) {
  if (!rows.length) return <p className="text-caption text-ink-3">Нет данных.</p>
  const max = Math.max(1, ...rows.map((r) => r.value))
  return (
    <ul className="space-y-2">
      {rows.map((r) => (
        <li key={r.label} className="grid grid-cols-[minmax(0,8.5rem)_minmax(0,1fr)_auto] items-center gap-3 text-sm sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)_auto]" title={r.hint}>
          <span className="truncate text-ink-2">{r.label}</span>
          <span className="h-2 rounded-full bg-sunken" aria-hidden>
            <span className={cx('block h-2 rounded-full', BAR_TONE[tone])} style={{ width: `${(r.value / max) * 100}%`, minWidth: r.value ? 4 : 0 }} />
          </span>
          <span className="min-w-10 text-right tabular-nums text-ink">{formatValue ? formatValue(r.value) : r.value}</span>
        </li>
      ))}
    </ul>
  )
}

/** Weekday × hour heat map of sends: the deeper the accent, the more responses went out in that hour. */
export function Heatmap({ cells, weekdays }: { cells: { weekday: number; hour: number; sent: number; invited: number }[]; weekdays: string[] }) {
  const max = Math.max(1, ...cells.map((c) => c.sent))
  const map = new Map(cells.map((c) => [`${c.weekday}-${c.hour}`, c]))
  const size = 16
  return (
    <div className="overflow-x-auto">
      <svg width={28 + 24 * size} height={7 * size + 18} className="block" role="img" aria-label="Отправленные отклики по дню недели и часу">
        {Array.from({ length: 24 }, (_, h) => (
          <text key={h} x={28 + h * size + size / 2} y={10} fontSize={10} textAnchor="middle" className="fill-ink-3" style={NUM}>
            {h % 3 === 0 ? h : ''}
          </text>
        ))}
        {weekdays.map((w, wi) => (
          <g key={w}>
            <text x={0} y={18 + wi * size + size - 4} fontSize={11} className="fill-ink-3">
              {w}
            </text>
            {Array.from({ length: 24 }, (_, h) => {
              const c = map.get(`${wi}-${h}`)
              const v = c?.sent ?? 0
              return (
                <rect key={h} x={28 + h * size} y={18 + wi * size} width={size - 2} height={size - 2} rx={2} className={v ? 'fill-accent' : 'fill-sunken'} fillOpacity={v ? 0.15 + 0.85 * (v / max) : 1}>
                  <title>{`${w} ${h}:00 — отправлено ${v}, приглашений ${c?.invited ?? 0}`}</title>
                </rect>
              )
            })}
          </g>
        ))}
      </svg>
    </div>
  )
}
