import { useEffect, useState } from 'react'
import { apiGet } from '../api/client'
import type { Ref } from '../api/types'
import { Tag, TAG_FIELD } from './TagInput'

/** Multi-select backed by an hh.ru suggest endpoint (areas, professional roles). */
export default function RefPicker({
  value,
  onChange,
  endpoint,
  placeholder,
  label,
}: {
  value: Ref[]
  onChange: (next: Ref[]) => void
  endpoint: string
  placeholder?: string
  label?: string
}) {
  const [text, setText] = useState('')
  const [options, setOptions] = useState<Ref[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const q = text.trim()
    if (q.length < 2) {
      setOptions([])
      return
    }
    const handle = setTimeout(() => {
      apiGet<Ref[]>(`${endpoint}?text=${encodeURIComponent(q)}`)
        .then((r) => {
          setOptions(r.filter((o) => !value.some((v) => v.id === o.id)).slice(0, 8))
          setError(null)
        })
        .catch((e) => setError(String(e?.message ?? e)))
    }, 300)
    return () => clearTimeout(handle)
  }, [text, endpoint, value])

  const pick = (o: Ref) => {
    onChange([...value, o])
    setText('')
    setOptions([])
  }

  return (
    <div className="relative">
      <div className={TAG_FIELD}>
        {value.map((v) => (
          <Tag key={v.id} onRemove={() => onChange(value.filter((x) => x.id !== v.id))} removeLabel={`Убрать ${v.name || v.id}`}>
            {v.name || v.id}
          </Tag>
        ))}
        <input
          aria-label={label}
          className="min-w-[9rem] flex-1 bg-transparent px-1 py-0.5 text-sm text-ink outline-none placeholder:text-ink-3"
          value={text}
          placeholder={value.length ? '' : placeholder}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter takes the first suggestion; Esc closes the list
            if (e.key === 'Enter' && options.length) {
              e.preventDefault()
              pick(options[0])
            } else if (e.key === 'Escape' && options.length) {
              e.preventDefault()
              setOptions([])
            }
          }}
        />
      </div>
      {(options.length > 0 || error) && (
        <div className="absolute z-20 mt-1 w-full rounded-lg border border-line bg-surface p-1 shadow-pop">
          {error && <div className="px-2.5 py-1.5 text-caption text-bad">{error}</div>}
          {options.map((o) => (
            <button key={o.id} type="button" className="block w-full rounded-md px-2.5 py-1.5 text-left text-sm text-ink transition-colors hover:bg-sunken focus-visible:bg-sunken" onClick={() => pick(o)}>
              {o.name}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
