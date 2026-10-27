import { describe, expect, it } from 'vitest'
import { clockTime, countOf, plural, shortAgo, shortDate } from './format'

describe('plural', () => {
  const forms: [string, string, string] = ['вакансия', 'вакансии', 'вакансий']
  it.each([
    [1, 'вакансия'],
    [2, 'вакансии'],
    [5, 'вакансий'],
    [11, 'вакансий'],
    [12, 'вакансий'],
    [21, 'вакансия'],
    [22, 'вакансии'],
    [111, 'вакансий'],
    [0, 'вакансий'],
  ])('%i → %s', (n, form) => expect(plural(n, forms)).toBe(form))
})

describe('countOf', () => {
  it('groups the digits the Russian way', () => {
    expect(countOf(2841, ['вакансия', 'вакансии', 'вакансий']).replace(/\s/g, ' ')).toBe('2 841 вакансия')
  })
})

describe('shortAgo', () => {
  const now = new Date('2026-10-22T12:00:00Z')
  it.each([
    ['2026-10-22T11:59:40Z', 'сейчас'],
    ['2026-10-22T11:35:00Z', '25 мин'],
    ['2026-10-22T07:00:00Z', '5 ч'],
    ['2026-10-19T12:00:00Z', '3 дн.'],
  ])('%s → %s', (iso, text) => expect(shortAgo(iso, now)).toBe(text))

  it('turns into a date after two weeks', () => {
    expect(shortAgo('2026-09-30T12:00:00Z', now)).toBe(shortDate('2026-09-30T12:00:00Z', now))
  })

  it('has a dash for nothing', () => {
    expect(shortAgo(null, now)).toBe('—')
    expect(shortAgo('не дата', now)).toBe('—')
  })
})

describe('shortDate', () => {
  const now = new Date('2026-10-22T12:00:00')
  it('drops the year of this year and keeps another one', () => {
    expect(shortDate('2026-03-05T12:00:00', now)).toBe('05.03')
    expect(shortDate('2025-12-31T12:00:00', now)).toBe('31.12.25')
  })
})

describe('clockTime', () => {
  const now = new Date('2026-10-23T14:00:00')
  it('shows the time today and the date with it on another day', () => {
    expect(clockTime('2026-10-23T23:05:00', now)).toBe('23:05')
    expect(clockTime('2026-10-24T02:05:00', now)).toBe('24.10 02:05')
    expect(clockTime(null, now)).toBe('—')
    expect(clockTime('не дата', now)).toBe('—')
  })
})
