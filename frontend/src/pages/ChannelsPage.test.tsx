import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import ChannelsPage from './ChannelsPage'

const VIEW = { items: [], interval_min: 60, languages: [], running: false }

describe('ChannelsPage', () => {
  it.each([
    [1, 'новый пост'],
    [2, 'новых поста'],
    [5, 'новых постов'],
    [21, 'новый пост'],
  ])('says %i %s in the header', async (n, words) => {
    stubApi({ '/api/channels/posts': { items: [], total: 0, page: 1, per_page: 50, new_count: n }, '/api/channels': VIEW })
    renderAt('/channels', '/channels', <ChannelsPage />)
    expect(await screen.findByText(`0 каналов · ${n} ${words} · проверка каждые 60 мин`)).toBeInTheDocument()
  })
})
