// How a vacancy reads in the list and in its card: short labels, the status badge, the stages it went through and
// the filters that hide part of the list. Pure functions — the page renders, this decides.
import type { SavedSearch, SelectionProfile, VacancyDetail, VacancyRow } from '../api/types'
import type { BadgeTone } from '../components/ui/Badge'
import type { TrackStep } from '../components/ui/PipelineTrack'
import { plural } from './format'
import { currencySign, money } from './money'
import { DEFAULT_FILTERS, FRESH_DAYS, GROUPS, SITES, type Filters } from './vacancyFilters'

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

/** «Вакансия недоступна» in the response flow (the assistant's panel, the board): what the button does. */
export const UNAVAILABLE_HINT = 'hh.ru пишет, что вакансия недоступна: отклик отменится, вакансия уйдёт в «Скрытые»'

/** The site of a vacancy: hh.ru or HireHi. Rows without the field are hh.ru (older answers). */
export function siteOf(v: { source?: string | null; source_label?: string | null } | null | undefined): string {
  if (v?.source_label) return v.source_label
  return v?.source === 'hirehi' ? 'HireHi' : 'hh.ru'
}

export function isHireHi(v: { source?: string | null } | null | undefined): boolean {
  return v?.source === 'hirehi'
}

/** The filter «Сайт» takes the site where the vacancy lives: one HireHi took from hh.ru is found on hh.ru and handled
 *  there, its HireHi row is a duplicate among the skipped ones. */
export const SITE_HINT = 'Вакансия, которую HireHi взял с hh.ru, считается вакансией hh.ru.'

/** UNAVAILABLE_HINT for the site of the vacancy. */
export function unavailableHint(site: string): string {
  return UNAVAILABLE_HINT.replace('hh.ru', site)
}

/** How a response to a HireHi vacancy goes: the original link is given by HireHi to a signed-in user only. */
export function hirehiApplyHint(label: string | null | undefined): string {
  return `Отклик — на странице HireHi: кнопка «${label || 'Отклик'}» откроет сайт работодателя, Telegram или почту. Нужен вход на HireHi.`
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
  /** what the chip means in full (a tooltip) */
  title?: string
  clear: (f: Filters) => Filters
}

const DAYS_AFTER_DO: [string, string, string] = ['дня', 'дней', 'дней'] // «до 1 дня», «до 3 дней», «до 5 дней»

/** The days of the freshness filter in the saved filters; '' (off) gives the default for the switch's label. */
export function freshDays(f: Pick<Filters, 'fresh_days'>): number {
  return Number(f.fresh_days) || FRESH_DAYS
}

/** The switch and the chip of the freshness filter. City names are not declined («кроме Казани» would need the
 *  genitive of any city the questionnaire may hold), so the label says «другие города» and the hint names them.
 *  Without a city in the questionnaire every city is subject. `homeAreas` undefined — not loaded yet. */
export function freshLabel(days: number, homeAreas?: string[]): string {
  const span = `${days} ${plural(days, DAYS_AFTER_DO)}`
  return homeAreas && homeAreas.length === 0 ? `Не старше ${span}` : `Другие города: до ${span}`
}

/** What the freshness filter never hides: the cities of the questionnaire and the vacancies with a response under way. */
export function freshHint(homeAreas?: string[]): string {
  if (!homeAreas) return 'Без срока: города из регионов анкеты и вакансии с откликом в работе.'
  if (homeAreas.length === 0) return 'Без срока только вакансии с откликом в работе: в анкете не указаны регионы.'
  return `Без срока: ${homeAreas.join(', ')} (регионы анкеты) и вакансии с откликом в работе.`
}

/** How many the freshness filter took out of the list: «69 старых скрыто», «1 старая скрыта», «3 старые скрыты». */
export function staleText(n: number): string {
  return `${n.toLocaleString('ru-RU')} ${plural(n, ['старая скрыта', 'старые скрыты', 'старых скрыто'])}`
}

const num = (n: number) => n.toLocaleString('ru-RU')

/** The switch and the chip of the responses cap: «Откликов до 1 000» (at most so many). */
export function responsesLabel(cap: number): string {
  return `Откликов до ${num(cap)}`
}

/** What the responses cap hides and what it never hides: a vacancy without the counter of hh.ru (nothing to compare)
 *  and one with a response under way. `cap` null — the field is empty, the filter is off. */
export function responsesHint(cap: number | null): string {
  const spared = 'Без счётчика откликов и с откликом в работе видны всегда.'
  return cap === null ? `Пусто — без ограничения. ${spared}` : `Скрыты вакансии, где откликов на hh.ru больше ${num(cap)}. ${spared}`
}

/** How many the responses cap took out of the list: «9 скрыто по числу откликов», «1 скрыта…», «3 скрыты…». */
export function cappedText(n: number): string {
  return `${num(n)} ${plural(n, ['скрыта', 'скрыты', 'скрыто'])} по числу откликов`
}

/** The empty list when only the standing filters (freshness, the responses cap) emptied it. The cap counts only what
 *  the freshness filter left (the list says the same), so the two numbers never overlap. */
export function scopeEmpty(o: { stale: number; capped: number; days: number; cap: number | null; homeAreas?: string[] }): { title: string; text: string } {
  const span = `${o.days} ${plural(o.days, DAYS_AFTER_DO)}` // «старше 1 дня», «старше 5 дней»
  const what = o.homeAreas && o.homeAreas.length === 0 ? 'Вакансий' : 'Вакансий из других городов'
  const capWords = `откликов больше ${num(o.cap ?? 0)}`
  if (o.capped > 0 && o.stale > 0) {
    return {
      title: `Свежих вакансий с откликами до ${num(o.cap ?? 0)} нет`,
      text: `${what} старше ${span}: ${num(o.stale)}; свежих, где ${capWords}: ${num(o.capped)}. Фильтры их скрывают.`,
    }
  }
  if (o.capped > 0) return { title: `Вакансий с откликами до ${num(o.cap ?? 0)} нет`, text: `Вакансий, где ${capWords}: ${num(o.capped)}. Фильтр их скрывает.` }
  return { title: 'Свежих вакансий нет', text: `${what} старше ${span}: ${num(o.stale)}. Фильтр их скрывает.` }
}

/** Everything that hides part of the list besides the tab and the text query (that one is in the search field):
 *  a chip each, so a filter saved last week can not empty the list unnoticed. The standing filters come first — the
 *  freshness filter (on by default, its chip stays while it hides something) and the responses cap. */
export function filterChips(f: Filters, searches?: SavedSearch[], sets?: SelectionProfile[], homeAreas?: string[]): FilterChip[] {
  const chips: FilterChip[] = []
  if (f.fresh_days) {
    chips.push({
      key: 'fresh',
      label: freshLabel(freshDays(f), homeAreas),
      title: freshHint(homeAreas),
      clear: (x) => ({ ...x, fresh_days: '' }),
    })
  }
  if (f.max_responses) {
    const cap = Number(f.max_responses)
    chips.push({ key: 'responses', label: responsesLabel(cap), title: responsesHint(cap), clear: (x) => ({ ...x, max_responses: '' }) })
  }
  if (f.search_id) {
    const name = searches?.find((s) => String(s.id) === f.search_id)?.name ?? `№${f.search_id}`
    chips.push({ key: 'search', label: `Поиск: ${name}`, clear: (x) => ({ ...x, search_id: '' }) })
  }
  if (f.source) {
    const name = SITES.find(([id]) => id === f.source)?.[1] ?? f.source
    chips.push({ key: 'site', label: `Сайт: ${name}`, title: SITE_HINT, clear: (x) => ({ ...x, source: '' }) })
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

/** The filters back to the defaults, keeping the tab and the sort order (they never hide anything by surprise). The
 *  freshness filter is a default: a reset turns it back on; the responses cap is off by default: a reset clears it. */
export function clearFilters(f: Filters): Filters {
  return { ...DEFAULT_FILTERS, group: f.group, sort: f.sort }
}

/** Whether «Сбросить» would change anything: with the freshness filter on by default its chip alone is no reason. */
export function canResetFilters(f: Filters): boolean {
  const reset = clearFilters(f)
  return (Object.keys(reset) as (keyof Filters)[]).some((k) => f[k] !== reset[k])
}
