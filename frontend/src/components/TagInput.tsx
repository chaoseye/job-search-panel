import { X } from 'lucide-react'
import { useState, type ReactNode } from 'react'

/** The look of a field that holds tags and a text input: one frame, the focus ring on the frame. */
export const TAG_FIELD =
  'flex min-h-[34px] w-full flex-wrap items-center gap-1 rounded-md border border-line-strong bg-surface px-1.5 py-1 text-sm transition-colors hover:border-ink-3 ' +
  'focus-within:border-focus focus-within:outline-2 focus-within:outline-focus/30 pointer-coarse:min-h-10'

export function Tag({ children, onRemove, removeLabel }: { children: ReactNode; onRemove: () => void; removeLabel: string }) {
  return (
    <span className="inline-flex h-6 max-w-full items-center gap-0.5 rounded-sm bg-sunken pl-1.5 pr-0.5 text-caption text-ink">
      <span className="truncate">{children}</span>
      <button type="button" className="grid size-5 place-items-center rounded-sm text-ink-3 transition-colors hover:bg-selected hover:text-ink" onClick={onRemove} aria-label={removeLabel} title={removeLabel}>
        <X aria-hidden className="size-3" />
      </button>
    </span>
  )
}

/** A list of words typed into one field: Enter or a comma adds, Backspace on an empty field removes the last one. */
export default function TagInput({
  value,
  onChange,
  placeholder,
  label,
}: {
  value: string[]
  onChange: (next: string[]) => void
  placeholder?: string
  label?: string
}) {
  const [draft, setDraft] = useState('')

  const commit = () => {
    const parts = draft
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
    if (parts.length) {
      const next = [...value]
      for (const p of parts) if (!next.includes(p)) next.push(p)
      onChange(next)
    }
    setDraft('')
  }

  return (
    <div className={TAG_FIELD}>
      {value.map((tag) => (
        <Tag key={tag} onRemove={() => onChange(value.filter((v) => v !== tag))} removeLabel={`Удалить ${tag}`}>
          {tag}
        </Tag>
      ))}
      <input
        aria-label={label}
        className="min-w-[8rem] flex-1 bg-transparent px-1 py-0.5 text-sm text-ink outline-none placeholder:text-ink-3"
        value={draft}
        placeholder={value.length ? '' : placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault()
            commit()
          } else if (e.key === 'Backspace' && !draft && value.length) {
            onChange(value.slice(0, -1))
          }
        }}
        onBlur={commit}
      />
    </div>
  )
}
