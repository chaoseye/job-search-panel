import { useEffect, useRef, type KeyboardEvent, type RefObject } from 'react'
import { focusables } from './floating'

// Open modal layers, the topmost last: Esc closes only the top one (a confirmation over a card closes alone).
const stack: object[] = []
// the page scroll as it was before the first layer locked it; the last layer to close gives it back, whatever order
// the layers unmount in (a card unmounted with a confirmation still open over it)
let pageOverflow = ''

/** A sheet or a dialog is open: page-wide keys (the board's J/K, Enter, Esc) leave the keyboard to it. */
export function hasOpenLayer(): boolean {
  return stack.length > 0
}

/** What a modal layer (sheet, dialog) does while it is open: takes focus and gives it back to what opened it, locks
 *  the page scroll, closes on Esc when it is the top layer and nothing inside took the key. Returns the Tab handler
 *  that keeps focus inside the panel. */
export function useModalLayer(panel: RefObject<HTMLElement | null>, close: () => void): (e: KeyboardEvent) => void {
  const token = useRef({})
  const onClose = useRef(close)
  onClose.current = close

  useEffect(() => {
    const me = token.current
    const html = document.documentElement
    if (!stack.length) {
      pageOverflow = html.style.overflow
      html.style.overflow = 'hidden'
    }
    stack.push(me)
    const before = document.activeElement as HTMLElement | null
    panel.current?.focus({ preventScroll: true })
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented && stack[stack.length - 1] === me) {
        e.preventDefault()
        onClose.current()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      stack.splice(stack.indexOf(me), 1)
      if (!stack.length) html.style.overflow = pageOverflow
      before?.focus?.({ preventScroll: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (e: KeyboardEvent) => {
    if (e.key !== 'Tab' || !panel.current) return
    const els = focusables(panel.current)
    if (!els.length) return
    const first = els[0]
    const last = els[els.length - 1]
    if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }
}
