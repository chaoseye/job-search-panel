import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { apiGet } from '../../api/client'
import type { BoardSource } from '../../api/types'
import { neighbourIn } from '../../lib/boardSources'
import { hasOpenLayer } from '../ui/modal'

export const PAGE = 60

interface Page<I> {
  key: string
  band: string | null
  total: number
  items: I[]
}

/** The state a board keeps in the address and around it: the open column and its band (`?src=tenders&col=new&band=…`,
 *  the back button closes a column opened from the board), the page of the open column, the focused card of each
 *  list, the toast and the narrow column a card has just moved to. Each pipeline's board uses it with its own
 *  columns, bands and actions. */
export function useBoardNav<I, P extends Page<I>>({
  source,
  columnKeys,
  bands,
  defaultBand,
  itemKey,
}: {
  source: BoardSource
  columnKeys: readonly string[]
  bands: readonly string[]
  defaultBand: string
  itemKey: (item: I) => number
}) {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const rawCol = params.get('col')
  const col = rawCol && columnKeys.includes(rawCol) ? rawCol : null
  const rawBand = params.get('band')
  const band = col === 'new' ? (rawBand && bands.includes(rawBand) ? rawBand : defaultBand) : null

  const [limit, setLimit] = useState(PAGE)
  useEffect(() => setLimit(PAGE), [col, band])
  const listKey = ['board-column', source, col, band, limit] as const
  const query = new URLSearchParams()
  if (source !== 'vacancies') query.set('source', source)
  if (band) query.set('band', band)
  query.set('limit', String(limit))
  const list = useQuery({
    queryKey: listKey,
    queryFn: () => apiGet<P>(`/api/board/column/${col}?${query.toString()}`),
    enabled: col !== null,
    refetchInterval: 30000,
    // «Показать ещё» keeps the cards on screen while the longer page loads; another column starts clean (a generic
    // function: react-query's placeholder type cannot be checked against a generic page)
    placeholderData: <T>(prev: T | undefined): T | undefined => {
      const page = prev as Page<I> | undefined
      return page && page.key === col && page.band === band ? prev : undefined
    },
  })

  // the focused card of each open list («new:hi», «sent»…): Enter acts on it, J/K move it
  const [focusBy, setFocusBy] = useState<Record<string, number>>({})
  const focusKey = `${col}:${band ?? ''}`
  const items: I[] = list.data?.items ?? []
  const focus = Math.max(0, Math.min(focusBy[focusKey] ?? 0, items.length - 1))
  const setFocus = (i: number) => setFocusBy((f) => ({ ...f, [focusKey]: Math.max(0, Math.min(i, items.length - 1)) }))

  const base = () => {
    const next = new URLSearchParams()
    if (source !== 'vacancies') next.set('src', source)
    return next
  }
  const growFrom = useRef<DOMRect | null>(null)
  // the first column opened by ←/→ or its narrow column comes back to the band it was left at
  const lastBand = useRef<string | null>(null)
  useEffect(() => {
    if (band) lastBand.current = band
  }, [band])
  const openColumn = (key: string, nextBand?: string, from?: Element | null) => {
    growFrom.current = from?.getBoundingClientRect() ?? null
    const next = base()
    next.set('col', key)
    const keep = nextBand ?? lastBand.current
    if (key === 'new' && keep) next.set('band', keep)
    if (col) setParams(next, { replace: true, state: location.state })
    else setParams(next, { state: { boardOpened: true } }) // from the board: the back button returns to it
  }
  const closeColumn = () => {
    if ((location.state as { boardOpened?: boolean } | null)?.boardOpened) navigate(-1)
    else setParams(base(), { replace: true })
  }
  const setBand = (b: string) => {
    const next = base()
    next.set('col', 'new')
    next.set('band', b)
    setParams(next, { replace: true, state: location.state })
  }

  // a toast says what happened (a longer one stays longer); a narrow column glows when a card lands in it
  const [said, say] = useState<string | null>(null)
  useEffect(() => {
    if (!said) return
    const t = window.setTimeout(() => say(null), Math.max(2600, said.length * 55))
    return () => window.clearTimeout(t)
  }, [said])
  const [arrived, setArrived] = useState<string | null>(null)
  useEffect(() => {
    if (!arrived) return
    const t = window.setTimeout(() => setArrived(null), 800)
    return () => window.clearTimeout(t)
  }, [arrived])

  // the card leaves the open list at once and the next one takes the focus; the server's answer refreshes the rest
  const removeFromList = (id: number) =>
    qc.setQueryData<P>(listKey, (old) => (old ? { ...old, total: Math.max(0, old.total - 1), items: old.items.filter((x) => itemKey(x) !== id) } : old))

  /** J/K and ↑/↓ walk the list, ←/→ open the neighbours, Esc brings the board back; true when the key was one of them. */
  const walk = (e: KeyboardEvent): boolean => {
    if (!col) return false
    if (e.code === 'KeyJ' || e.key === 'ArrowDown') setFocus(focus + 1)
    else if (e.code === 'KeyK' || e.key === 'ArrowUp') setFocus(focus - 1)
    else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const key = neighbourIn(columnKeys, col, e.key === 'ArrowRight' ? 1 : -1)
      if (key) openColumn(key)
    } else if (e.key === 'Escape') closeColumn()
    else return false
    return true
  }

  return {
    col,
    band,
    list,
    items,
    focus,
    setFocus,
    growFrom,
    openColumn,
    closeColumn,
    setBand,
    more: () => setLimit((n) => n + PAGE),
    said,
    say,
    arrived,
    setArrived,
    removeFromList,
    walk,
  }
}

/** The keys of an open column, listened on the window; a card or a dialog over the board, a field, a menu keep theirs,
 *  and a focused button or link keeps Enter and the space. `handle` returns true for a key it used. */
export function useBoardKeys(active: boolean, handle: (e: KeyboardEvent) => boolean) {
  useEffect(() => {
    if (!active) return
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || hasOpenLayer()) return
      const t = e.target
      if (t instanceof Element && t.closest('input, textarea, select, [contenteditable="true"], [role="menu"]')) return
      if ((t instanceof HTMLButtonElement || t instanceof HTMLAnchorElement) && (e.key === 'Enter' || e.key === ' ')) return
      if (handle(e)) e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })
}
