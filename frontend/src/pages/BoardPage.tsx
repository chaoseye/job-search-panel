import { useSearchParams } from 'react-router-dom'
import StudioBoard from '../components/board/StudioBoard'
import TenderBoard from '../components/board/TenderBoard'
import VacancyBoard from '../components/board/VacancyBoard'
import { isSource } from '../lib/boardSources'

/** «Воронка» (variant «Б + Б3»): the board of a pipeline with its funnel over it, switched over the board —
 *  vacancies (the address without `src`), tenders of workspace.ru and studios (`?src=tenders`,
 *  `?src=studios`). Each board opens a column right in place as a working list. */
export default function BoardPage() {
  const [params] = useSearchParams()
  const raw = params.get('src')
  const source = isSource(raw) ? raw : 'vacancies'
  if (source === 'tenders') return <TenderBoard />
  if (source === 'studios') return <StudioBoard />
  return <VacancyBoard />
}
