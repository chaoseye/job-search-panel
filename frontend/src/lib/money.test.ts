import { describe, expect, it } from 'vitest'
import { money, salaryRange } from './money'

const nbsp = (s: string) => s.replace(/ | /g, ' ') // ru-RU groups thousands with a (narrow) no-break space

describe('money', () => {
  it('formats with the currency sign', () => {
    expect(nbsp(money(150000, 'RUR'))).toBe('150 000 ₽')
    expect(nbsp(money(2500.6, 'usd'))).toBe('2 501 $')
    expect(money(null, 'RUR')).toBe('')
    expect(nbsp(money(100, 'XYZ'))).toBe('100 XYZ')
  })
})

describe('salaryRange', () => {
  it('reads like hh.ru', () => {
    expect(nbsp(salaryRange({ from: 100000, to: 150000, currency: 'RUR', gross: false }))).toBe('100 000 ₽ – 150 000 ₽')
    expect(nbsp(salaryRange({ from: 100000, to: null, currency: 'RUR', gross: true }))).toBe('от 100 000 ₽ до вычета')
    expect(nbsp(salaryRange({ from: null, to: 90000, currency: 'RUR', gross: null }))).toBe('до 90 000 ₽')
    expect(salaryRange({ from: null, to: null, currency: null, gross: null })).toBe('—')
  })
})
