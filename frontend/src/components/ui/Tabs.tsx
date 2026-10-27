import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Count } from './Badge'
import { cx } from './cx'

// the strip fades out on the side where tabs are hidden, so a cut tab never looks like the last one
const FADE = {
  none: '',
  start: '[mask-image:linear-gradient(to_left,black_calc(100%-2rem),transparent)]',
  end: '[mask-image:linear-gradient(to_right,black_calc(100%-2rem),transparent)]',
  both: '[mask-image:linear-gradient(to_right,transparent,black_2rem,black_calc(100%-2rem),transparent)]',
}

/** Which ends of a sideways-scrolling strip hide something; follows scrolling, resizing of the strip and of its
 *  items (a count that arrived, the font that loaded) and a change of the items (`content`). */
function useHiddenEnds(el: HTMLElement | null, content: string): keyof typeof FADE {
  const [ends, setEnds] = useState<keyof typeof FADE>('none')
  useEffect(() => {
    if (!el) return
    const update = () => {
      const start = el.scrollLeft > 1
      const end = el.scrollWidth - el.clientWidth - el.scrollLeft > 1
      setEnds(start && end ? 'both' : start ? 'start' : end ? 'end' : 'none')
    }
    update()
    el.addEventListener('scroll', update, { passive: true })
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update)
    if (ro) for (const node of [el, ...Array.from(el.children)]) ro.observe(node)
    return () => {
      el.removeEventListener('scroll', update)
      ro?.disconnect()
    }
  }, [el, content])
  return ends
}

export interface TabItem {
  id: string
  label: string
  count?: number
  /** the count is a queue that waits for the user (accent) rather than a plain total */
  attention?: boolean
}

/** Underline tabs: the views of one list. Arrow keys move between them (and select, like the platform tabs). */
export function Tabs({
  items,
  value,
  onChange,
  label,
  trailing,
  className,
}: {
  items: TabItem[]
  value: string
  onChange: (id: string) => void
  label: string
  trailing?: ReactNode
  className?: string
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const [strip, setStrip] = useState<HTMLDivElement | null>(null)
  const content = items.map((t) => `${t.id}:${t.label}:${t.count ?? ''}`).join('|')
  const ends = useHiddenEnds(strip, content)
  const selected = items.findIndex((t) => t.id === value)

  // the selected tab is always in sight, also when it was picked from a link or by the arrow keys, and after the
  // counts arrive (a list reloading for the new tab can show the strip without them for a moment)
  useEffect(() => {
    const tab = refs.current[selected]
    if (!strip || !tab) return
    const box = strip.getBoundingClientRect()
    const r = tab.getBoundingClientRect()
    if (r.left < box.left) strip.scrollLeft -= box.left - r.left + 32
    else if (r.right > box.right) strip.scrollLeft += r.right - box.right + 32
  }, [strip, selected, content])

  const onKey = (e: KeyboardEvent, i: number) => {
    const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
    if (!step) return
    e.preventDefault()
    const next = (i + step + items.length) % items.length
    refs.current[next]?.focus()
    onChange(items[next].id)
  }
  return (
    <div className={cx('flex items-end gap-1 border-b border-line', className)}>
      <div ref={setStrip} role="tablist" aria-label={label} className={cx('-mb-px flex min-w-0 items-end gap-5 overflow-x-auto [scrollbar-width:none]', FADE[ends])}>
        {items.map((t, i) => {
          const on = t.id === value
          return (
            <button
              key={t.id}
              ref={(el) => {
                refs.current[i] = el
              }}
              type="button"
              role="tab"
              aria-selected={on}
              tabIndex={on ? 0 : -1}
              onClick={() => onChange(t.id)}
              onKeyDown={(e) => onKey(e, i)}
              className={cx(
                'flex h-10 shrink-0 items-center gap-1 whitespace-nowrap border-b-2 text-sm transition-colors',
                'focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus',
                on ? 'border-ink font-medium text-ink' : 'border-transparent text-ink-2 hover:text-ink',
              )}
            >
              {t.label}
              {/* the space keeps the accessible name «Кандидаты 83», not «Кандидаты83» (a flex gap is not text) */}
              {t.count !== undefined && (
                <>
                  {' '}
                  <Count value={t.count} attention={t.attention && t.count > 0} />
                </>
              )}
            </button>
          )
        })}
      </div>
      {trailing && <div className="-mb-px ml-auto flex shrink-0 items-end">{trailing}</div>}
    </div>
  )
}
