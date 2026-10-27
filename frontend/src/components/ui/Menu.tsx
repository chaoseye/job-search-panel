import { Check, Download, ExternalLink } from 'lucide-react'
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode, type Ref } from 'react'
import { createPortal } from 'react-dom'
import { cx } from './cx'
import { useDismiss, useFloating } from './floating'

export interface MenuItemDef {
  key: string
  label: ReactNode
  onSelect?: () => void
  /** a link to another site: opens in a new tab */
  href?: string
  /** with href: a file of the panel to save (a CSV export), not a page to open */
  download?: boolean
  tone?: 'danger'
  disabled?: boolean
  /** a choice among several (sort order): shows a check on the chosen one */
  checked?: boolean
  hint?: ReactNode
}
export type MenuEntry = MenuItemDef | 'separator' | { section: string }

export interface MenuTriggerProps {
  ref: Ref<HTMLButtonElement>
  onClick: () => void
  onKeyDown: (e: KeyboardEvent) => void
  'aria-haspopup': 'menu'
  'aria-expanded': boolean
  'aria-controls'?: string
}

const ITEM =
  'flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm outline-none transition-colors hover:bg-sunken focus:bg-sunken pointer-coarse:py-2.5'

/** Dropdown of actions or choices. Keyboard: ↓ opens, ↑/↓/Home/End move, Enter picks, Esc closes back to the trigger.
 *  Rendered in a portal, so a table or a sheet never clips it. */
export function Menu({
  trigger,
  items,
  label,
  align = 'end',
}: {
  trigger: (props: MenuTriggerProps) => ReactNode
  items: MenuEntry[]
  label: string
  align?: 'start' | 'end'
}) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const id = useId()
  const pos = useFloating(open, triggerRef, menuRef, align)
  const placed = pos !== null
  useDismiss(open, [triggerRef, menuRef], () => setOpen(false))

  const enabled = () => Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]:not([aria-disabled="true"])') ?? [])
  useEffect(() => {
    if (open && placed) (enabled().find((el) => el.getAttribute('aria-checked') === 'true') ?? enabled()[0])?.focus()
  }, [open, placed])

  const close = () => {
    setOpen(false)
    triggerRef.current?.focus()
  }
  const onMenuKey = (e: KeyboardEvent) => {
    const els = enabled()
    const i = els.indexOf(document.activeElement as HTMLElement)
    const go = (n: number) => {
      e.preventDefault()
      els[(n + els.length) % els.length]?.focus()
    }
    if (e.key === 'ArrowDown') go(i + 1)
    else if (e.key === 'ArrowUp') go(i - 1)
    else if (e.key === 'Home') go(0)
    else if (e.key === 'End') go(els.length - 1)
    else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      close()
    } else if (e.key === 'Tab') setOpen(false)
  }
  const pick = (item: MenuItemDef) => {
    if (item.disabled) return
    close()
    item.onSelect?.()
  }

  return (
    <>
      {trigger({
        ref: triggerRef,
        onClick: () => setOpen((o) => !o),
        onKeyDown: (e) => {
          if (e.key === 'ArrowDown' && !open) {
            e.preventDefault()
            setOpen(true)
          }
        },
        'aria-haspopup': 'menu',
        'aria-expanded': open,
        'aria-controls': open ? id : undefined,
      })}
      {open &&
        createPortal(
          <div
            ref={menuRef}
            id={id}
            role="menu"
            aria-label={label}
            onKeyDown={onMenuKey}
            style={{ top: pos?.top ?? 0, left: pos?.left ?? 0, transformOrigin: pos?.origin, visibility: placed ? 'visible' : 'hidden' }}
            className="fixed z-50 min-w-52 max-w-[min(24rem,calc(100vw-16px))] rounded-lg border border-line bg-surface p-1 font-ui text-ink shadow-pop transition-[opacity,transform] duration-150 ease-out-strong starting:scale-[0.97] starting:opacity-0 motion-reduce:transition-none"
          >
            {items.map((it, i) => {
              if (it === 'separator') return <div key={`sep-${i}`} role="separator" className="mx-1 my-1 h-px bg-line" />
              if ('section' in it)
                return (
                  <div key={`sec-${i}`} className="px-2.5 pb-1 pt-2 text-xs font-medium text-ink-3">
                    {it.section}
                  </div>
                )
              if (it.href)
                return it.download ? (
                  <a key={it.key} role="menuitem" href={it.href} download onClick={() => setOpen(false)} className={cx(ITEM, 'text-ink')}>
                    <span className="min-w-0 flex-1">
                      {it.label}
                      {it.hint && <span className="block text-xs text-ink-3">{it.hint}</span>}
                    </span>
                    <Download aria-hidden className="size-3.5 text-ink-3" />
                  </a>
                ) : (
                  <a key={it.key} role="menuitem" href={it.href} target="_blank" rel="noreferrer" onClick={() => setOpen(false)} className={cx(ITEM, 'text-ink')}>
                    <span className="min-w-0 flex-1">{it.label}</span>
                    <ExternalLink aria-hidden className="size-3.5 text-ink-3" />
                  </a>
                )
              const radio = it.checked !== undefined
              return (
                <button
                  key={it.key}
                  type="button"
                  role={radio ? 'menuitemradio' : 'menuitem'}
                  aria-checked={radio ? it.checked : undefined}
                  aria-disabled={it.disabled || undefined}
                  onClick={() => pick(it)}
                  className={cx(ITEM, it.tone === 'danger' ? 'text-bad' : 'text-ink', it.disabled && 'cursor-not-allowed opacity-50')}
                >
                  {radio && <Check aria-hidden className={cx('size-4 shrink-0', it.checked ? 'text-ink' : 'text-transparent')} />}
                  <span className="min-w-0 flex-1">
                    {it.label}
                    {it.hint && <span className="block text-xs text-ink-3">{it.hint}</span>}
                  </span>
                </button>
              )
            })}
          </div>,
          document.body,
        )}
    </>
  )
}
