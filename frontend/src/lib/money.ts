const SYMBOLS: Record<string, string> = { RUR: '₽', RUB: '₽', USD: '$', EUR: '€', KZT: '₸', BYR: 'Br', BYN: 'Br' }

export function currencySign(currency: string | null | undefined): string {
  return SYMBOLS[(currency ?? 'RUR').toUpperCase()] ?? currency ?? ''
}

export function money(amount: number | null | undefined, currency: string | null | undefined): string {
  if (amount === null || amount === undefined) return ''
  return `${Math.round(amount).toLocaleString('ru-RU')} ${currencySign(currency)}`.trim()
}

export function salaryRange(s: { from: number | null; to: number | null; currency: string | null; gross: boolean | null }): string {
  if (!s.from && !s.to) return '—'
  const cur = s.currency
  let text = ''
  if (s.from && s.to) text = `${money(s.from, cur)} – ${money(s.to, cur)}`
  else if (s.from) text = `от ${money(s.from, cur)}`
  else text = `до ${money(s.to, cur)}`
  return s.gross ? `${text} до вычета` : text
}
