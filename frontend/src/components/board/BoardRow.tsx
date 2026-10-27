import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { cx } from '../ui/cx'
import { useSwipe } from './useSwipe'

const MARKER = /\[\[\s*уточнить:?\s*([^\]]*)\]\]/gi

/** A letter with Claude's questions marked: «[[УТОЧНИТЬ: сколько лет]]» reads «сколько лет?» on a warm background. */
export function LetterText({ text, className }: { text: string; className?: string }) {
  const parts: ReactNode[] = []
  let at = 0
  for (const m of text.matchAll(MARKER)) {
    parts.push(text.slice(at, m.index))
    parts.push(
      <mark key={m.index} className="rounded bg-warn-soft px-1 font-medium text-warn">
        {(m[1] || 'уточнить').trim()}?
      </mark>,
    )
    at = (m.index ?? 0) + m[0].length
  }
  parts.push(text.slice(at))
  return <div className={cx('whitespace-pre-wrap text-sm leading-relaxed text-ink-2', className)}>{parts}</div>
}

/** A long text (a letter, an answer, a description) folded to a few lines under a fade (a line clamp would end on a
 *  lone «…» where a blank line parts the paragraphs); «Весь текст» unfolds it in place. */
export function LetterPreview({ text, lines }: { text: string; lines: number }) {
  const box = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [long, setLong] = useState(false)
  useLayoutEffect(() => {
    const el = box.current
    if (el && !open) setLong(el.scrollHeight > el.clientHeight + 1)
  }, [text, lines, open])
  return (
    <div className="rounded-lg border border-line bg-surface p-3">
      <div
        ref={box}
        className={cx('overflow-hidden text-sm leading-relaxed', !open && long && '[mask-image:linear-gradient(to_bottom,#000_55%,transparent)]')}
        style={open ? undefined : { maxHeight: `${lines * 1.625}em` }}
      >
        <LetterText text={text} />
      </div>
      {long && (
        <button type="button" onClick={() => setOpen((o) => !o)} className="mt-1.5 rounded text-caption font-medium text-accent hover:underline focus-visible:outline-2 focus-visible:outline-focus">
          {open ? 'Свернуть' : 'Весь текст'}
        </button>
      )}
    </div>
  )
}

/** The buttons of the focused card: at its foot, and stuck to the foot of the list while the card is taller than the
 *  list, so the decision is in sight whatever part of the reasons or the letter is being read. */
export function ActionBar({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('sticky bottom-0 z-[1] flex flex-wrap items-center gap-2 border-t border-rule bg-canvas py-3', className)}>{children}</div>
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="ml-1 rounded border border-current px-1 text-[10.5px] font-medium leading-4 opacity-60 max-md:hidden">{children}</kbd>
}

/** A list of points (Claude's reasons, the fit of a tender); with a limit the first ones and «ещё N». */
export function Reasons({ title, items, limit }: { title: string; items: string[]; limit?: number }) {
  const [all, setAll] = useState(false)
  const shown = limit && !all ? items.slice(0, limit) : items
  return (
    <div>
      <div className="text-xs font-medium text-ink-3">{title}</div>
      <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-ink-2">
        {shown.map((x) => (
          <li key={x}>{x}</li>
        ))}
      </ul>
      {shown.length < items.length && (
        <button type="button" onClick={() => setAll(true)} className="mt-1 ml-5 rounded text-caption font-medium text-accent hover:underline focus-visible:outline-2 focus-visible:outline-focus">
          Ещё {items.length - shown.length}
        </button>
      )}
    </div>
  )
}

/** The facts of a card in a line: «Москва · удалённо · откликов 13». */
export function Facts({ items, className }: { items: (string | null | undefined | false)[]; className?: string }) {
  const shown = items.filter(Boolean)
  if (shown.length === 0) return null
  return <div className={cx('text-caption text-ink-3', className)}>{shown.join(' · ')}</div>
}

export interface SwipeStep {
  label: string
  run: () => void
}

/** One card of an open column, whatever the pipeline: a line to scan (score, title, meta, badges, age), and under the
 *  focused one its details. A phone swipes the row: right — the next step, left — skip. */
export function BoardRow({
  id,
  focused,
  narrow,
  score,
  title,
  meta,
  badges,
  aside,
  right,
  left,
  onFocus,
  children,
}: {
  id: number
  focused: boolean
  narrow: boolean
  score: ReactNode
  title: string
  meta: (string | null | undefined | false)[]
  badges: ReactNode[]
  aside?: string | null
  right?: SwipeStep | null
  left?: SwipeStep | null
  onFocus: () => void
  children?: ReactNode
}) {
  const swipe = useSwipe({ onRight: narrow && right ? right.run : undefined, onLeft: narrow && left ? left.run : undefined })
  const shown = meta.filter(Boolean)
  return (
    <li className={cx('relative border-b border-rule', focused && 'bg-canvas shadow-[inset_3px_0_0_var(--color-accent)]')} data-board-row={id} aria-current={focused || undefined}>
      <div className="relative overflow-hidden">
        {narrow && (
          <div aria-hidden className="absolute inset-0 flex items-center justify-between bg-sunken px-4 text-caption font-medium">
            <span className="text-good">{right ? `${right.label} →` : ''}</span>
            <span className="text-ink-3">{left ? `← ${left.label}` : ''}</span>
          </div>
        )}
        <div
          {...swipe}
          role="button"
          tabIndex={-1}
          onClick={onFocus}
          className={cx(
            'relative flex cursor-pointer select-none items-center gap-3 px-4 py-2.5 transition-[transform,background-color] duration-200 ease-out-strong',
            narrow && 'touch-pan-y',
            focused ? 'bg-canvas' : 'bg-surface hover:bg-sunken',
          )}
        >
          {score}
          <div className="min-w-0 flex-1">
            <div className="line-clamp-2 text-sm font-medium text-ink @2xl:line-clamp-1">{title}</div>
            <div className="truncate text-caption text-ink-3">
              {shown.map((c, i) => (
                <span key={i}>
                  {i > 0 && ' · '}
                  {c}
                </span>
              ))}
            </div>
            {/* badges stand at the right of a wide column; a narrow one (a phone) gives the title the width */}
            {badges.length > 0 && <div className="mt-1 flex flex-wrap gap-1 @2xl:hidden">{badges}</div>}
          </div>
          {badges.length > 0 && <div className="hidden shrink-0 items-center gap-1.5 @2xl:flex">{badges}</div>}
          {aside && <span className="shrink-0 text-caption text-ink-3 max-sm:hidden">{aside}</span>}
        </div>
      </div>
      {focused && children}
    </li>
  )
}
