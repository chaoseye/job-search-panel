import { Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import { cx, hasWidth } from './cx'
import { Input } from './Field'

/** Search field with a magnifier; the query goes out a moment after the typing stops, not on every key. */
export function SearchInput({
  value,
  onChange,
  label,
  placeholder,
  className,
  delay = 250,
}: {
  value: string
  onChange: (q: string) => void
  label: string
  placeholder?: string
  className?: string
  delay?: number
}) {
  const [text, setText] = useState(value)
  useEffect(() => setText(value), [value])
  useEffect(() => {
    if (text === value) return
    const t = window.setTimeout(() => onChange(text), delay)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text])
  return (
    <div className={cx('relative', hasWidth(className) ? null : 'w-full sm:w-72', className)}>
      <Search aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-ink-3" />
      <Input type="search" aria-label={label} placeholder={placeholder ?? label} value={text} onChange={(e) => setText(e.target.value)} className="pl-8" />
    </div>
  )
}
