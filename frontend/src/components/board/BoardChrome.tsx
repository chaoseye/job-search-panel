import { useSearchParams } from 'react-router-dom'
import type { BoardSource } from '../../api/types'
import { SOURCES } from '../../lib/boardSources'
import { Segmented } from '../ui/Segmented'

/** Which pipeline the board shows: vacancies (the address without `src`), tenders, studios. Another board starts on
 *  its whole board, an open column of the previous one closes. */
export function SourceSwitch({ value }: { value: BoardSource }) {
  const [, setParams] = useSearchParams()
  return <Segmented label="Что на доске" options={SOURCES} value={value} onChange={(s) => setParams(s === 'vacancies' ? {} : { src: s }, { replace: true })} />
}

/** What the last action did, at the foot of the screen for a moment. */
export function Toast({ said }: { said: string | null }) {
  return (
    <div role="status" aria-live="polite" className="pointer-events-none fixed inset-x-0 bottom-5 z-40 flex justify-center px-4">
      {said && <div className="max-w-xl rounded-lg bg-ink px-3.5 py-2 text-caption text-surface shadow-pop">{said}</div>}
    </div>
  )
}
