import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode, type Ref } from 'react'
import { createPortal } from 'react-dom'
import { cx } from './cx'
import { useDismiss, useFloating } from './floating'
import { useMediaQuery } from './useMediaQuery'

export interface PopoverTriggerProps {
  ref: Ref<HTMLButtonElement>
  onClick: () => void
  'aria-haspopup': 'dialog'
  'aria-expanded': boolean
  'aria-controls'?: string
}

/** A small panel attached to its trigger (filters, «add by link», «skip with a reason»); a bottom sheet on a phone.
 *  Focus goes into it; Esc or a press outside closes it and gives focus back to the trigger. */
export function Popover({
  trigger,
  children,
  label,
  align = 'start',
  className,
}: {
  trigger: (props: PopoverTriggerProps) => ReactNode
  children: (close: () => void) => ReactNode
  label: string
  align?: 'start' | 'end'
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const narrow = useMediaQuery('(max-width: 639px)')
  const triggerRef = useRef<HTMLButtonElement>(null)
  const layerRef = useRef<HTMLDivElement>(null)
  const id = useId()
  const pos = useFloating(open && !narrow, triggerRef, layerRef, align)
  const ready = open && (narrow || pos !== null)
  useDismiss(open && !narrow, [triggerRef, layerRef], () => setOpen(false))

  // once shown, focus goes to the field marked data-autofocus (one that expects typing), else to the panel itself;
  // React's autoFocus would fire while the panel is still hidden for measuring and get lost
  useEffect(() => {
    const el = layerRef.current
    if (!ready || !el || el.contains(document.activeElement)) return
    ;(el.querySelector<HTMLElement>('[data-autofocus]') ?? el).focus({ preventScroll: true })
  }, [ready])

  const close = () => {
    setOpen(false)
    triggerRef.current?.focus()
  }
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close()
    }
  }

  const panel = (
    <div
      ref={layerRef}
      id={id}
      role="dialog"
      aria-label={label}
      tabIndex={-1}
      onKeyDown={onKey}
      style={narrow ? undefined : { top: pos?.top ?? 0, left: pos?.left ?? 0, transformOrigin: pos?.origin, visibility: pos ? 'visible' : 'hidden' }}
      className={cx(
        'font-ui text-ink outline-none',
        narrow
          ? 'fixed inset-x-0 bottom-0 z-50 max-h-[85dvh] overflow-y-auto rounded-t-xl bg-surface p-4 pb-[max(1rem,env(safe-area-inset-bottom))] shadow-pop transition-transform duration-[280ms] ease-drawer starting:translate-y-full motion-reduce:transition-none'
          : 'fixed z-50 max-h-[calc(100dvh-16px)] overflow-y-auto rounded-lg border border-line bg-surface p-4 shadow-pop transition-[opacity,transform] duration-150 ease-out-strong starting:scale-[0.97] starting:opacity-0 motion-reduce:transition-none',
        !narrow && (className ?? 'w-[360px]'),
      )}
    >
      {children(close)}
    </div>
  )

  return (
    <>
      {trigger({ ref: triggerRef, onClick: () => setOpen((o) => !o), 'aria-haspopup': 'dialog', 'aria-expanded': open, 'aria-controls': open ? id : undefined })}
      {open &&
        createPortal(
          narrow ? (
            <div className="fixed inset-0 z-50">
              <div aria-hidden className="absolute inset-0 bg-ink/25 transition-opacity duration-200 starting:opacity-0" onClick={close} />
              {panel}
            </div>
          ) : (
            panel
          ),
          document.body,
        )}
    </>
  )
}
