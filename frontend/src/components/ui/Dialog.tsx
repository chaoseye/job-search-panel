import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { cx } from './cx'
import { prefersReducedMotion } from './floating'
import { useModalLayer } from './modal'

const EXIT_MS = 150

/** A decision that stops everything else (consent before the first response): centred over a dimmed page, focus
 *  inside, Esc or a press on the dimmed page cancels. `children` gets `close`, which plays the exit first. */
export function Dialog({ onClose, labelledBy, children, className }: { onClose: () => void; labelledBy: string; children: (close: () => void) => ReactNode; className?: string }) {
  const panel = useRef<HTMLDivElement>(null)
  const [closing, setClosing] = useState(false)
  const timer = useRef<number | undefined>(undefined)
  const close = useCallback(() => {
    if (closing) return
    if (prefersReducedMotion()) {
      onClose()
      return
    }
    setClosing(true)
    timer.current = window.setTimeout(onClose, EXIT_MS)
  }, [closing, onClose])
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const trapFocus = useModalLayer(panel, close)

  return createPortal(
    <div className="fixed inset-0 z-50 grid place-items-center p-4 font-ui text-ink">
      <div aria-hidden onClick={close} className={cx('absolute inset-0 bg-ink/30 transition-opacity duration-200 ease-out-strong starting:opacity-0', closing && 'opacity-0')} />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        tabIndex={-1}
        onKeyDown={trapFocus}
        className={cx(
          'relative max-h-[calc(100dvh-2rem)] w-full max-w-lg overflow-y-auto rounded-xl bg-surface p-5 shadow-pop outline-none sm:p-6',
          'transition-[transform,opacity] duration-200 ease-out-strong starting:scale-[0.97] starting:opacity-0 motion-reduce:transition-opacity',
          closing && 'scale-[0.97] opacity-0 duration-150',
          className,
        )}
      >
        {children(close)}
      </div>
    </div>,
    document.body,
  )
}
