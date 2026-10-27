import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Input, Select, Textarea } from './Field'

// two utilities of one kind do not override each other by their order in the class list, so a control must drop
// its own default when the caller sizes it
describe('control width', () => {
  it('fills its row and may shrink by default', () => {
    render(<Input aria-label="Поиск" />)
    expect(screen.getByLabelText('Поиск')).toHaveClass('w-full', 'min-w-0')
  })

  it('keeps the size the caller sets', () => {
    render(
      <>
        <Input aria-label="С" className="w-[9.5rem]" />
        <Textarea aria-label="Текст" className="min-h-40" />
        <Select aria-label="Способ" wrapperClassName="w-full sm:w-56">
          <option>Любой способ</option>
        </Select>
      </>,
    )
    const from = screen.getByLabelText('С')
    expect(from).toHaveClass('w-[9.5rem]')
    expect(from).not.toHaveClass('w-full')

    const text = screen.getByLabelText('Текст')
    expect(text).toHaveClass('min-h-40')
    expect(text).not.toHaveClass('min-h-24')

    const wrapper = screen.getByLabelText('Способ').parentElement!
    expect(wrapper.className.split(' ').filter((c) => c === 'w-full')).toHaveLength(1)
    expect(wrapper).toHaveClass('sm:w-56')
  })
})
