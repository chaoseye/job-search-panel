import { X } from 'lucide-react'
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Button } from './Button'
import { cx } from './cx'
import { prefersReducedMotion } from './floating'
import { useModalLayer } from './modal'

const SheetContext = createContext<{ close: () => void }>({ close: () => {} })

/** Close the surrounding sheet with its exit motion (instead of unmounting it on the spot). */
export const useSheet = () => useContext(SheetContext)

const EXIT_MS = 180
const SIZES = {
  md: 'sm:max-w-[520px]',
  lg: 'sm:max-w-[min(780px,calc(100vw-48px))]',
  /** the navigation on a phone: a strip of the page stays visible to tap away */
  nav: 'max-w-[300px]',
}

/** Side panel over the page (vacancy, response, studio, tender cards; the navigation on a phone). Slides in from its
 *  edge and leaves the same way; dims the page; keeps focus inside and gives it back to what opened it; Esc closes —
 *  menus and popovers inside take Esc first. Full width on a phone. */
export function Sheet({
  onClose,
  labelledBy,
  children,
  side = 'right',
  size = 'lg',
  className,
}: {
  onClose: () => void
  labelledBy: string
  children: ReactNode
  side?: 'right' | 'left'
  size?: keyof typeof SIZES
  className?: string
}) {
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

  const right = side === 'right'
  return createPortal(
    <SheetContext.Provider value={{ close }}>
      <div className="fixed inset-0 z-40 font-ui text-ink">
        <div
          aria-hidden
          onClick={close}
          className={cx('absolute inset-0 bg-ink/25 transition-opacity duration-200 ease-out-strong starting:opacity-0', closing && 'opacity-0')}
        />
        <div
          ref={panel}
          role="dialog"
          aria-modal="true"
          aria-labelledby={labelledBy}
          tabIndex={-1}
          onKeyDown={trapFocus}
          className={cx(
            'absolute inset-y-0 flex w-full flex-col bg-surface outline-none',
            right ? 'right-0 shadow-sheet' : 'left-0 shadow-[16px_0_40px_-12px_oklch(24.5%_0.022_264/0.22)]',
            SIZES[size],
            'transition-[transform,opacity] duration-[280ms] ease-drawer motion-reduce:duration-150',
            right ? 'starting:translate-x-full' : 'starting:-translate-x-full',
            'motion-reduce:starting:translate-x-0 motion-reduce:starting:opacity-0',
            closing && (right ? 'translate-x-full' : '-translate-x-full'),
            closing && 'duration-[180ms] motion-reduce:translate-x-0 motion-reduce:opacity-0',
            className,
          )}
        >
          {children}
        </div>
      </div>
    </SheetContext.Provider>,
    document.body,
  )
}

/** The × of a sheet header. */
export function SheetCloseButton({ label = 'Закрыть' }: { label?: string }) {
  const { close } = useSheet()
  return (
    <Button variant="ghost" iconOnly onClick={close} aria-label={label} title={`${label} (Esc)`}>
      <X aria-hidden />
    </Button>
  )
}
