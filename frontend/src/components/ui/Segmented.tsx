import { cx, hasClass } from './cx'

/** Single choice among a few short options (period, work format): a track with the chosen option raised. The options
 *  wrap to the next line; `flex-nowrap overflow-x-auto` from the caller keeps them in one line that scrolls (a phone). */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
  className,
}: {
  options: [T, string][]
  value: T
  onChange: (v: T) => void
  label: string
  className?: string
}) {
  return (
    <div role="radiogroup" aria-label={label} className={cx('inline-flex max-w-full gap-0.5 rounded-md bg-sunken p-0.5', !hasClass(className, 'flex-nowrap') && 'flex-wrap', className)}>
      {options.map(([id, text]) => (
        <button
          key={id}
          type="button"
          role="radio"
          aria-checked={value === id}
          onClick={() => onChange(id)}
          className={cx(
            'h-7 whitespace-nowrap rounded-[5px] px-2.5 text-caption transition-colors pointer-coarse:h-9',
            'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus',
            value === id ? 'bg-surface font-medium text-ink shadow-[0_1px_2px_oklch(24.5%_0.022_264/0.12)]' : 'text-ink-2 hover:text-ink',
          )}
        >
          {text}
        </button>
      ))}
    </div>
  )
}
