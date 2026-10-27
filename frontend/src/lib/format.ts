export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const diffMs = d.getTime() - Date.now()
  const abs = Math.abs(diffMs)
  const rtf = new Intl.RelativeTimeFormat('ru-RU', { numeric: 'auto' })
  const minutes = Math.round(diffMs / 60000)
  if (abs < 60 * 60000) return rtf.format(minutes, 'minute')
  const hours = Math.round(diffMs / 3600000)
  if (abs < 48 * 3600000) return rtf.format(hours, 'hour')
  const days = Math.round(diffMs / 86400000)
  return rtf.format(days, 'day')
}

/** Russian plural form: plural(3, ['вакансия', 'вакансии', 'вакансий']) → «вакансии». */
export function plural(n: number, forms: [string, string, string]): string {
  const a = Math.abs(n) % 100
  const b = a % 10
  if (a > 10 && a < 20) return forms[2]
  if (b > 1 && b < 5) return forms[1]
  if (b === 1) return forms[0]
  return forms[2]
}

/** «12 вакансий» with the number grouped the Russian way. */
export function countOf(n: number, forms: [string, string, string]): string {
  return `${n.toLocaleString('ru-RU')} ${plural(n, forms)}`
}

/** Short age for dense rows: «5 мин», «3 ч», «6 дн.», then the date «13.10» (or «13.10.25» from another year). */
export function shortAgo(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const min = Math.round((now.getTime() - d.getTime()) / 60000)
  if (min < 1) return 'сейчас'
  if (min < 60) return `${min} мин`
  const h = Math.round(min / 60)
  if (h < 24) return `${h} ч`
  const days = Math.round(h / 24)
  if (days < 14) return `${days} дн.`
  return shortDate(iso, now)
}

/** «13.10», with the year when it is not this year: «13.10.25». */
export function shortDate(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const dd = String(d.getDate()).padStart(2, '0')
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  return d.getFullYear() === now.getFullYear() ? `${dd}.${mm}` : `${dd}.${mm}.${String(d.getFullYear()).slice(2)}`
}

/** «14:05» today, «24.10 02:05» on another day: when something ends (a session, a link). */
export function clockTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return d.toDateString() === now.toDateString() ? hhmm : `${shortDate(iso, now)} ${hhmm}`
}
