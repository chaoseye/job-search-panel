import { useEffect, useLayoutEffect, useState, type RefObject } from 'react'

export interface FloatingPosition {
  top: number
  left: number
  /** transform-origin: the layer grows out of its trigger */
  origin: string
}

/** Fixed position of a layer (menu, popover) next to its trigger: below it, or above when there is no room; kept 8 px
 *  inside the viewport. Recomputed on resize and on any scroll, so the layer follows a trigger inside a scrolling sheet. */
export function useFloating(
  open: boolean,
  anchor: RefObject<HTMLElement | null>,
  layer: RefObject<HTMLElement | null>,
  align: 'start' | 'end' = 'start',
  offset = 6,
): FloatingPosition | null {
  const [pos, setPos] = useState<FloatingPosition | null>(null)
  useLayoutEffect(() => {
    if (!open) {
      setPos(null)
      return
    }
    const place = () => {
      const a = anchor.current?.getBoundingClientRect()
      const el = layer.current
      if (!a || !el) return
      const w = el.offsetWidth
      const h = el.offsetHeight
      const vw = document.documentElement.clientWidth
      const vh = window.innerHeight
      let left = align === 'end' ? a.right - w : a.left
      left = Math.max(8, Math.min(left, vw - w - 8))
      let top = a.bottom + offset
      let vertical = 'top'
      if (top + h > vh - 8) {
        if (a.top - offset - h >= 8) {
          top = a.top - offset - h
          vertical = 'bottom'
        } else top = Math.max(8, vh - 8 - h) // taller than the room on either side: pinned to the bottom edge
      }
      setPos({ top, left, origin: `${vertical} ${align === 'end' ? 'right' : 'left'}` })
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open, anchor, layer, align, offset])
  return pos
}

/** Closes a layer on a pointer press outside both the trigger and the layer. */
export function useDismiss(open: boolean, refs: RefObject<HTMLElement | null>[], onDismiss: () => void) {
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node
      if (refs.some((r) => r.current?.contains(target))) return
      onDismiss()
    }
    document.addEventListener('pointerdown', onDown, true)
    return () => document.removeEventListener('pointerdown', onDown, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
}

/** Elements that take keyboard focus inside a container, in tab order. */
export function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(
    root.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((el) => !el.closest('[inert]') && el.getAttribute('aria-hidden') !== 'true')
}

/** True when the user asked the system for less motion. */
export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}
