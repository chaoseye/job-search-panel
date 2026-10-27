import { describe, expect, it } from 'vitest'
import { cx, hasClass, hasWidth } from './cx'

describe('cx', () => {
  it('joins the truthy class names', () => {
    expect(cx('flex', false, null, undefined, '', 'gap-2')).toBe('flex gap-2')
  })
})

describe('hasClass', () => {
  it('finds a utility of the kind, with or without variants', () => {
    expect(hasWidth('w-16')).toBe(true)
    expect(hasWidth('flex sm:w-72')).toBe(true)
    expect(hasWidth('max-sm:w-full')).toBe(true)
    expect(hasWidth('w-[9.5rem]')).toBe(true)
    expect(hasClass('text-sm min-h-40', 'min-h-')).toBe(true)
    expect(hasClass('sm:min-w-[11rem]', 'min-w-')).toBe(true)
  })

  it('does not take a longer utility for one of the kind', () => {
    expect(hasWidth('min-w-0 max-w-3xl size-4')).toBe(false)
    expect(hasClass('min-w-0', 'w-')).toBe(false)
    expect(hasWidth(undefined)).toBe(false)
    expect(hasWidth('')).toBe(false)
  })
})
