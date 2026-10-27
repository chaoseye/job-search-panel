import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { Dialog } from './Dialog'
import { Sheet } from './Sheet'

/** A card in a sheet with a confirmation that opens over it, like a vacancy card asking for consent. */
function Card({ onClose }: { onClose: () => void }) {
  const [confirm, setConfirm] = useState(false)
  return (
    <Sheet labelledBy="card-title" onClose={onClose}>
      <h2 id="card-title">Карточка</h2>
      <button type="button" onClick={() => setConfirm(true)}>
        Отправить
      </button>
      {confirm && (
        <Dialog labelledBy="confirm-title" onClose={() => setConfirm(false)}>
          {(close) => (
            <>
              <h2 id="confirm-title">Подтверждение</h2>
              <button type="button" onClick={close}>
                Отмена
              </button>
            </>
          )}
        </Dialog>
      )}
    </Sheet>
  )
}

function Page() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Открыть карточку
      </button>
      {open && <Card onClose={() => setOpen(false)} />}
    </>
  )
}

const pageScroll = () => document.documentElement.style.overflow

describe('modal layers', () => {
  it('Esc closes only the top layer; focus and the page scroll come back as the layers close', async () => {
    render(<Page />)
    const opener = screen.getByRole('button', { name: 'Открыть карточку' })
    opener.focus()
    fireEvent.click(opener)
    expect(screen.getByRole('dialog', { name: 'Карточка' })).toHaveFocus()
    expect(pageScroll()).toBe('hidden')

    const send = screen.getByRole('button', { name: 'Отправить' })
    send.focus()
    fireEvent.click(send)
    expect(screen.getByRole('dialog', { name: 'Подтверждение' })).toHaveFocus()

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Подтверждение' })).toBeNull())
    expect(screen.getByRole('dialog', { name: 'Карточка' })).toBeInTheDocument()
    expect(send).toHaveFocus()
    expect(pageScroll()).toBe('hidden')

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Карточка' })).toBeNull())
    expect(opener).toHaveFocus()
    expect(pageScroll()).toBe('')
  })

  it('gives the page scroll back when the card goes away with the confirmation still open', () => {
    const { unmount } = render(<Page />)
    fireEvent.click(screen.getByRole('button', { name: 'Открыть карточку' }))
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }))
    expect(screen.getByRole('dialog', { name: 'Подтверждение' })).toBeInTheDocument()
    expect(pageScroll()).toBe('hidden')
    unmount()
    expect(pageScroll()).toBe('')
  })
})
