import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderAt, stubApi } from '../test/render'
import SitesPage from './SitesPage'

const VIEW = { items: [], interval_min: 60, cities: [], unsupported: {}, running: false }

describe('SitesPage', () => {
  it.each([
    [1, 'новая вакансия'],
    [2, 'новые вакансии'],
    [5, 'новых вакансий'],
    [21, 'новая вакансия'],
  ])('says %i %s in the header', async (n, words) => {
    stubApi({ '/api/sites/vacancies': { items: [], total: 0, page: 1, per_page: 50, new_count: n }, '/api/sites': VIEW })
    renderAt('/sites', '/sites', <SitesPage />)
    expect(await screen.findByText(`0 сайтов · ${n} ${words} · проверка каждые 60 мин`)).toBeInTheDocument()
  })
})
