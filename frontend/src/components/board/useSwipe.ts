import { useRef, type PointerEvent as ReactPointerEvent } from 'react'
import { prefersReducedMotion } from '../ui/floating'

type El = HTMLElement

/** A row that follows the finger sideways (touch and pen; a mouse keeps its clicks and selection): past the threshold
 *  or after a quick flick it flies out to that side and runs the side's action, short of it springs back. A side
 *  without an action resists the finger. Vertical moves stay the page's: give the element `touch-action: pan-y`. */
export function useSwipe({ onRight, onLeft, threshold = 80 }: { onRight?: () => void; onLeft?: () => void; threshold?: number }) {
  const start = useRef<{ x: number; id: number } | null>(null)
  const last = useRef({ dx: 0, t: 0, v: 0 })

  function end(el: El, cancel: boolean) {
    if (!start.current) return
    start.current = null
    const { dx, v } = last.current
    el.style.transition = ''
    delete el.dataset.swipe
    // a flick counts as much as a long pull: the speed of the last move, px per ms
    const right = dx > threshold || (dx > 30 && v > 0.6)
    const left = dx < -threshold || (dx < -30 && v < -0.6)
    const action = cancel ? undefined : right ? onRight : left ? onLeft : undefined
    if (!action) {
      el.style.transform = ''
      return
    }
    el.style.transform = `translateX(${right ? 110 : -110}%)`
    window.setTimeout(action, prefersReducedMotion() ? 0 : 160)
    // the row usually leaves the list; if the action fails and it stays, bring it back
    window.setTimeout(() => {
      if (el.isConnected) el.style.transform = ''
    }, 700)
  }

  return {
    onPointerDown(e: ReactPointerEvent<El>) {
      if (e.pointerType === 'mouse' || e.button !== 0 || start.current) return
      if ((e.target as Element).closest('button, a, input, textarea, [contenteditable="true"]')) return
      start.current = { x: e.clientX, id: e.pointerId }
      last.current = { dx: 0, t: e.timeStamp, v: 0 }
      try {
        e.currentTarget.setPointerCapture(e.pointerId)
      } catch {
        /* the pointer is gone already */
      }
      e.currentTarget.style.transition = 'none'
    },
    onPointerMove(e: ReactPointerEvent<El>) {
      if (!start.current || e.pointerId !== start.current.id) return
      const dx = e.clientX - start.current.x
      const dt = Math.max(1, e.timeStamp - last.current.t)
      last.current = { dx, t: e.timeStamp, v: (dx - last.current.dx) / dt }
      if (Math.abs(dx) < 8) return
      const allowed = (dx > 0 && onRight) || (dx < 0 && onLeft)
      e.currentTarget.style.transform = `translateX(${allowed ? dx : dx * 0.25}px)`
      e.currentTarget.dataset.swipe = dx > 0 ? 'right' : 'left'
    },
    onPointerUp(e: ReactPointerEvent<El>) {
      end(e.currentTarget, false)
    },
    onPointerCancel(e: ReactPointerEvent<El>) {
      end(e.currentTarget, true)
    },
  }
}
