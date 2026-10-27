import { ChevronDown } from 'lucide-react'
import { forwardRef, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from 'react'
import { cx, hasClass, hasWidth } from './cx'

// One height for controls in a row (34 px, 40 on touch); the border never changes width — focus is an outline.
const CONTROL =
  'rounded-md border border-line-strong bg-surface text-sm text-ink placeholder:text-ink-3 transition-colors ' +
  'hover:border-ink-3 focus:border-focus focus:outline-2 focus:outline-offset-0 focus:outline-focus/30 ' +
  'disabled:cursor-not-allowed disabled:opacity-55'

// a control fills its row and may shrink in a flex row, unless the caller sizes it (w-16, sm:w-72, min-w-44)
const fill = (className?: string) => cx(hasWidth(className) ? null : 'w-full', hasClass(className, 'min-w-') ? null : 'min-w-0')

const INPUT = cx(CONTROL, 'h-[34px] px-3 pointer-coarse:h-10')

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...rest }, ref) {
  return <input ref={ref} className={cx(INPUT, fill(className), className)} {...rest} />
})

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea({ className, ...rest }, ref) {
  return <textarea ref={ref} className={cx(CONTROL, fill(className), hasClass(className, 'min-h-') ? null : 'min-h-24', 'resize-y px-3 py-2 leading-relaxed', className)} {...rest} />
})

/** Native select (keyboard and screen readers for free) with the chevron of the system; `wrapperClassName` sizes it. */
export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement> & { wrapperClassName?: string }>(function Select(
  { className, wrapperClassName, children, ...rest },
  ref,
) {
  return (
    <span className={cx('relative inline-flex', fill(wrapperClassName), wrapperClassName)}>
      <select ref={ref} className={cx(CONTROL, hasClass(className, 'min-w-') ? null : 'min-w-0', 'h-[34px] w-full cursor-pointer appearance-none truncate pl-3 pr-8 pointer-coarse:h-10', className)} {...rest}>
        {children}
      </select>
      <ChevronDown aria-hidden className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-ink-3" />
    </span>
  )
})

export function Checkbox({ label, className, ...rest }: InputHTMLAttributes<HTMLInputElement> & { label: ReactNode }) {
  return (
    <label className={cx('flex w-fit cursor-pointer items-center gap-2 text-sm text-ink', className)}>
      <input type="checkbox" className="size-4 shrink-0 cursor-pointer rounded-sm accent-accent" {...rest} />
      {label}
    </label>
  )
}

/** Label above the control, optional hint below (never a placeholder standing in for a label). */
export function Field({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cx('flex min-w-0 flex-col gap-1.5', className)}>
      <span className="text-caption font-medium text-ink-2">{label}</span>
      {children}
      {hint && <span className="text-xs text-ink-3">{hint}</span>}
    </label>
  )
}
