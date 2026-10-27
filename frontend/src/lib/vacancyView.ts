// How a vacancy reads in the list and in its card: short labels, the status badge, the stages it went through and
// the filters that hide part of the list. Pure functions — the page renders, this decides.
import type { SavedSearch, SelectionProfile, VacancyDetail, VacancyRow } from '../api/types'
import type { BadgeTone } from '../components/ui/Badge'
import type { TrackStep } from '../components/ui/PipelineTrack'
import { currencySign, money } from './money'
import { DEFAULT_FILTERS, GROUPS, type Filters } from './vacancyFilters'

/** hh experience levels; the filter keeps any combination (a comma-separated list in the query and in saved filters). */
export const EXPERIENCE_LEVELS: [string, string][] = [
  ['noExperience', 'без опыта'],
  ['between1And3', '1–3 года'],
  ['between3And6', '3–6 лет'],
  ['moreThan6', '6+ лет'],
]

/** The work format filter takes one value; '' is «any». */
export const WORK_FORMATS: [string, string][] = [
  ['', 'Любой'],
  ['REMOTE', 'Удалённо'],
  ['HYBRID', 'Гибрид'],
  ['ON_SITE', 'Офис'],
]

export const SORTS: [string, string][] = [
  ['score', 'По оценке'],
  ['published', 'Сначала свежие'],
  ['salary', 'Выше зарплата'],
  ['responses', 'Меньше откликов'],
]

/** The views worth a tab; the rest of GROUPS live in the «Ещё» menu, under these sections. */
export const PRIMARY_GROUPS: { id: string; label: string; attention?: boolean }[] = [
  { id: 'candidates', label: 'Кандидаты' },
  { id: 'needs_clarification', label: 'Уточнить письмо', attention: true },
  { id: 'needs_manual', label: 'Нужно действие', attention: true },
  { id: 'sent', label: 'Отправлено' },
  { id: 'watched', label: 'Наблюдение' },
]
export const MORE_GROUPS: { section: string; ids: string[] }[] = [
  { section: 'Кандидаты по оценке', ids: ['suitable', 'maybe', 'prescored'] },
  { section: 'Отклик', ids: ['queued', 'already_applied'] },
  { section: 'Отсеянные и архив', ids: ['not_suitable', 'filtered_out', 'skipped', 'hidden', 'new', 'all'] },
]

/** A group is called the same in its tab, in «Ещё», in the empty list and on the buttons of the classic view: the
 *  tab names win over the long ones of GROUPS. */
export const GROUP_LABEL: Record<string, string> = {
  ...Object.fromEntries(GROUPS),
  ...Object.fromEntries(PRIMARY_GROUPS.map((g) => [g.id, g.label])),
}

/** Groups whose count is a queue that waits for the user (the accent count). */
export const ATTENTION_GROUPS: ReadonlySet<string> = new Set(PRIMARY_GROUPS.filter((g) => g.attention).map((g) => g.id))

const EXPERIENCE_SHORT: Record<string, string> = {
  'Нет опыта': 'без опыта',
  'От 1 года до 3 лет': '1–3 года',
  'От 3 до 6 лет': '3–6 лет',
  'Более 6 лет': '6+ лет',
}

export function experienceShort(name: string | null): string | null {
  return name ? (EXPERIENCE_SHORT[name] ?? name.toLowerCase()) : null
}

const FORMAT_SHORT: Record<string, string> = { REMOTE: 'удалённо', HYBRID: 'гибрид', ON_SITE: 'офис', FIELD_WORK: 'разъездная' }

export function formatsShort(v: Pick<VacancyRow, 'work_format_ids' | 'work_formats'>): string {
  if (v.work_format_ids.length) return v.work_format_ids.map((id) => FORMAT_SHORT[id] ?? id.toLowerCase()).join(', ')
  return v.work_formats.map((f) => f.toLowerCase()).join(', ')
}

/** Marks that change how to apply. «Без зарплаты» is not one: the salary column already shows a dash. */
export function rowFlags(flags: string[]): { key: string; label: string }[] {
  const out: { key: string; label: string }[] = []
  if (flags.includes('test_required')) out.push({ key: 'test', label: 'тест обязателен' })
  else if (flags.includes('test')) out.push({ key: 'test', label: 'есть тест' })
  if (flags.includes('letter_required')) out.push({ key: 'letter', label: 'письмо обязательно' })
  if (flags.includes('direct')) out.push({ key: 'direct', label: 'внешний отклик' })
  return out
}

/** The net estimate (what lands on the card): «200 000 ₽». */
export function salaryNet(s: VacancyRow['salary']): string | null {
  return s.net_estimate ? money(s.net_estimate, s.currency) : null
}

/** The range as published, compact: «120–200 тыс. ₽», «от 80 тыс. ₽ до вычета»; small amounts as they are. */
export function salaryPublished(s: VacancyRow['salary']): string | null {
  if (!s.from && !s.to) return null
  const sign = currencySign(s.currency)
  const big = Math.max(s.from ?? 0, s.to ?? 0) >= 10000
  const n = (v: number) => (big ? Math.round(v / 1000).toLocaleString('ru-RU') : Math.round(v).toLocaleString('ru-RU'))
  const unit = big ? ` тыс. ${sign}` : ` ${sign}`
  let text: string
  if (s.from && s.to) text = s.from === s.to ? n(s.from) : `${n(s.from)}–${n(s.to)}`
  else if (s.from) text = `от ${n(s.from)}`
  else text = `до ${n(s.to!)}`
  return `${text}${unit}${s.gross ? ' до вычета' : ''}`
}

const LETTER_BADGE: Record<VacancyRow['letter_status'], { label: string; tone: BadgeTone }> = {
  ready: { label: 'Письмо готово', tone: 'good' },
  edited: { label: 'Письмо готово', tone: 'good' },
  template: { label: 'Письмо по шаблону', tone: 'neutral' },
  needs_clarification: { label: 'Уточнить письмо', tone: 'warn' },
  none: { label: 'Без письма', tone: 'neutral' },
}

const STATUS_BADGE: Record<string, { label: string; tone: BadgeTone }> = {
  new: { label: 'Не обработана', tone: 'neutral' },
  prescored: { label: 'Ждёт разбора', tone: 'neutral' },
  awaiting_claude: { label: 'В пакете Claude', tone: 'info' },
  queued: { label: 'В очереди', tone: 'info' },
  needs_manual: { label: 'Нужно действие', tone: 'warn' },
  sent: { label: 'Отклик отправлен', tone: 'neutral' },
  already_applied: { label: 'Отклик уже есть', tone: 'neutral' },
  skipped: { label: 'Пропущена', tone: 'neutral' },
  hidden: { label: 'Скрыта', tone: 'neutral' },
  filtered_out: { label: 'Отсеяна правилами', tone: 'neutral' },
  not_suitable: { label: 'Ниже порога', tone: 'neutral' },
  watched: { label: 'Наблюдение', tone: 'neutral' },
}

/** The state of the current cover letter as a badge. */
export function letterBadge(status: VacancyRow['letter_status']): { label: string; tone: BadgeTone } {
  return LETTER_BADGE[status] ?? LETTER_BADGE.none
}

/** One badge per row: the stage when the vacancy is not a candidate, else the state of its letter — the next step. */
export function rowStatus(v: Pick<VacancyRow, 'pipeline_status' | 'letter_status' | 'status_label'>): { label: string; tone: BadgeTone } {
  return STATUS_BADGE[v.pipeline_status] ?? LETTER_BADGE[v.letter_status] ?? { label: v.status_label, tone: 'neutral' }
}

const STEP_LABELS = ['Найдена', 'Отбор', 'Оценка Claude', 'Письмо', 'Отклик'] as const

function track(at: number, state: 'current' | 'attention' | 'done', upTo: number = STEP_LABELS.length): TrackStep[] {
  return STEP_LABELS.slice(0, upTo).map((label, i) => ({
    key: label,
    label,
    state: i < at ? 'done' : i === at ? state : 'todo',
  }))
}

/** The stages of a vacancy for its card: found → selected by the rules → evaluated by Claude → letter → response,
 *  or where it left the way (filtered out, below the threshold, skipped, hidden, watch-only). */
export function pipelineOf(d: Pick<VacancyDetail, 'pipeline_status' | 'score' | 'letter_status'>): {
  steps: TrackStep[]
  exit: { label: string; tone: BadgeTone } | null
} {
  const evaluated = d.score !== null
  const reached = evaluated ? 3 : 2 // steps behind a vacancy that left the way
  switch (d.pipeline_status) {
    case 'new':
      return { steps: track(1, 'current'), exit: null }
    case 'watched':
      return { steps: track(1, 'done', 1), exit: { label: 'Наблюдение, без разбора', tone: 'neutral' } }
    case 'filtered_out':
      return { steps: track(1, 'done', 1), exit: { label: 'Отсеяна правилами', tone: 'neutral' } }
    case 'not_suitable':
      return evaluated
        ? { steps: track(3, 'done', 3), exit: { label: 'Claude: не подходит', tone: 'neutral' } }
        : { steps: track(2, 'done', 2), exit: { label: 'Ниже порога предоценки', tone: 'neutral' } }
    case 'prescored':
    case 'awaiting_claude':
      return { steps: track(2, 'current'), exit: null }
    case 'needs_manual':
      return { steps: track(4, 'attention'), exit: null }
    case 'queued':
      return { steps: track(4, 'current'), exit: null }
    case 'sent':
    case 'already_applied':
      return { steps: track(5, 'done'), exit: null }
    case 'skipped':
      return { steps: track(reached, 'done', reached), exit: { label: 'Пропущена', tone: 'neutral' } }
    case 'hidden':
      return { steps: track(reached, 'done', reached), exit: { label: 'Скрыта', tone: 'neutral' } }
  }
  // a candidate with Claude's verdict: the letter is the next thing to do
  if (d.letter_status === 'needs_clarification') return { steps: track(3, 'attention'), exit: null }
  if (d.letter_status === 'none') return { steps: track(evaluated ? 3 : 2, 'current'), exit: null }
  return { steps: track(4, 'current'), exit: null }
}

export interface FilterChip {
  key: string
  label: string
  clear: (f: Filters) => Filters
}

/** Everything that hides part of the list besides the tab and the text query (that one is in the search field):
 *  a chip each, so a filter saved last week can not empty the list unnoticed. */
export function filterChips(f: Filters, searches?: SavedSearch[], sets?: SelectionProfile[]): FilterChip[] {
  const chips: FilterChip[] = []
  if (f.search_id) {
    const name = searches?.find((s) => String(s.id) === f.search_id)?.name ?? `№${f.search_id}`
    chips.push({ key: 'search', label: `Поиск: ${name}`, clear: (x) => ({ ...x, search_id: '' }) })
  }
  if (f.selection_profile_id) {
    const name = f.selection_profile_id === '0' ? 'основная анкета' : (sets?.find((s) => String(s.id) === f.selection_profile_id)?.name ?? `№${f.selection_profile_id}`)
    chips.push({ key: 'set', label: `Критерии: ${name}`, clear: (x) => ({ ...x, selection_profile_id: '' }) })
  }
  if (f.work_format) {
    const label = WORK_FORMATS.find(([id]) => id === f.work_format)?.[1] ?? f.work_format
    chips.push({ key: 'format', label: `Формат: ${label.toLowerCase()}`, clear: (x) => ({ ...x, work_format: '' }) })
  }
  for (const id of f.experience.split(',').filter(Boolean)) {
    const label = EXPERIENCE_LEVELS.find(([e]) => e === id)?.[1] ?? id
    chips.push({
      key: `exp-${id}`,
      label: `Опыт: ${label}`,
      clear: (x) => ({ ...x, experience: x.experience.split(',').filter((e) => e && e !== id).join(',') }),
    })
  }
  if (f.area.trim()) chips.push({ key: 'area', label: `Регион: ${f.area.trim()}`, clear: (x) => ({ ...x, area: '' }) })
  if (f.salary_from) chips.push({ key: 'salary', label: `На руки от ${money(Number(f.salary_from), 'RUR')}`, clear: (x) => ({ ...x, salary_from: '' }) })
  if (f.min_score) chips.push({ key: 'score', label: `Оценка от ${f.min_score}`, clear: (x) => ({ ...x, min_score: '' }) })
  if (f.unseen_only) chips.push({ key: 'unseen', label: 'Только новые', clear: (x) => ({ ...x, unseen_only: false }) })
  if (f.include_snoozed) chips.push({ key: 'snoozed', label: 'С отложенными', clear: (x) => ({ ...x, include_snoozed: false }) })
  return chips
}

/** The filters back to the defaults, keeping the tab and the sort order (they never hide anything by surprise). */
export function clearFilters(f: Filters): Filters {
  return { ...DEFAULT_FILTERS, group: f.group, sort: f.sort }
}
