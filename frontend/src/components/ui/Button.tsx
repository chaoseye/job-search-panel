import { forwardRef, type ButtonHTMLAttributes } from 'react'
import { cx } from './cx'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md'

// One look per role: primary is the single ink-filled action of a view, secondary is outlined, ghost is text with a
// hover tint, danger is ghost in the negative colour. The press scales down a little (feedback on pointer-down).
const BASE =
  'inline-flex shrink-0 select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-md font-medium ' +
  'transition-[background-color,border-color,color,transform] duration-150 ease-out-strong active:scale-[0.97] ' +
  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus ' +
  'disabled:pointer-events-none disabled:opacity-50 aria-disabled:pointer-events-none aria-disabled:opacity-50'

const SIZES: Record<ButtonSize, string> = {
  sm: 'h-7 px-2.5 text-caption [&_svg]:size-3.5',
  md: 'h-[34px] px-3.5 text-sm [&_svg]:size-4 pointer-coarse:h-10',
}
const ICON_SIZES: Record<ButtonSize, string> = {
  sm: 'size-7 [&_svg]:size-4',
  md: 'size-[34px] [&_svg]:size-[18px] pointer-coarse:size-10',
}
const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-ink text-surface hover:bg-ink-hover',
  secondary: 'border border-line-strong bg-surface text-ink hover:bg-sunken',
  ghost: 'text-ink-2 hover:bg-sunken hover:text-ink',
  danger: 'text-bad hover:bg-bad-soft',
}

/** Classes of a button, also for links that look like one (<a className={buttonClass('secondary')}>). */
export function buttonClass(variant: ButtonVariant = 'secondary', size: ButtonSize = 'md', iconOnly = false): string {
  return cx(BASE, iconOnly ? ICON_SIZES[size] : SIZES[size], VARIANTS[variant])
}

type Props = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: ButtonSize; iconOnly?: boolean }

export const Button = forwardRef<HTMLButtonElement, Props>(function Button(
  { variant = 'secondary', size = 'md', iconOnly = false, className, type = 'button', ...rest },
  ref,
) {
  return <button ref={ref} type={type} className={cx(buttonClass(variant, size, iconOnly), className)} {...rest} />
})
