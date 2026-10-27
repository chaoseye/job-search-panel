import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ApplicationRow, BoardColumnList, BoardItem, BoardView, VacancyDetail, VacancyRow } from '../api/types'
import detailFixture from '../test/vacancy_detail.json'
import { renderAt, stubApi } from '../test/render'
import BoardPage from './BoardPage'

function row(id: number, name: string, over: Partial<VacancyRow> = {}): VacancyRow {
  return {
    id,
    hh_id: String(1000 + id),
    name,
    employer: { id: '1', name: 'Компания', accredited_it: false, trusted: null },
    area_name: 'Москва',
    address: null,
    metro: [],
    salary: { from: null, to: null, currency: null, gross: null, net_estimate: null, net_rur: null },
    experience_name: null,
    employment_form_name: null,
    work_formats: [],
    work_format_ids: [],
    published_at: null,
    first_seen_at: '2026-10-20T10:00:00Z',
    responses_count: null,
    duplicate_of_id: null,
    sources: [],
    flags: [],
    pipeline_status: 'suitable',
    status_label: 'подходит',
    pipeline_reason: null,
    prescore: 60,
    score: 80,
    verdict: 'apply',
    letter_status: 'ready',
    batch_id: null,
    selection_profile_id: null,
    selection_profile_name: null,
    alternate_url: `https://hh.ru/vacancy/${1000 + id}`,
    apply_alternate_url: `https://hh.ru/applicant/vacancy_response?vacancyId=${1000 + id}`,
    is_new: false,
    snoozed_until: null,
    hidden: false,
    user_override: false,
    archived: false,
    has_full: true,
    ...over,
  }
}

function application(id: number, over: Partial<ApplicationRow> = {}): ApplicationRow {
  return {
    id,
    vacancy_id: id,
    vacancy: null,
    mode: 'assist',
    mode_label: 'ассистент',
    status: 'sent',
    status_label: 'отправлен',
    letter_text: 'Здравствуйте! Письмо, которое ушло.',
    queued_at: '2026-10-20T10:00:00Z',
    sent_at: '2026-10-20T10:05:00Z',
    hh_negotiation_id: null,
    negotiation_url: null,
    response_url: null,
    error_type: null,
    error_value: null,
    error_text: null,
    attempts: 1,
    confirmed_by_user: true,
    dry_run: false,
    autopilot: false,
    created_at: '2026-10-20T10:00:00Z',
    negotiation: null,
    ...over,
  }
}

function item(vacancy: VacancyRow, over: Partial<BoardItem> = {}): BoardItem {
  return { vacancy, application: null, stage_at: '2026-10-20T10:00:00Z', outcome: null, ...over }
}

// a letter ready to go and one with Claude's marker left in it (the server will not send that one)
const READY = row(101, 'Frontend-разработчик')
const MARKED = row(202, 'Аналитик данных', { score: 74, letter_status: 'needs_clarification' })

function detail(v: VacancyRow, text: string, problems: string[] = []): VacancyDetail {
  return {
    ...(detailFixture as unknown as VacancyDetail),
    ...v,
    evaluation: {
      id: v.id,
      batch_id: 'b1',
      score: v.score ?? 0,
      verdict: 'apply',
      summary: `Сводка Claude по вакансии ${v.name}`,
      matched: ['первое', 'второе', 'третье', 'четвёртое', 'пятое'],
      gaps: ['пресейл'],
      red_flags: [],
      salary_fit: null,
      seniority_fit: null,
      language: 'ru',
      profile_version: 1,
      stale_profile: false,
      user_feedback: null,
      user_comment: null,
      created_at: '2026-10-20T10:00:00Z',
    },
    letter: {
      id: v.id,
      version: 1,
      text,
      language: 'ru',
      char_count: text.length,
      origin: 'claude',
      batch_id: 'b1',
      mentions: [],
      needs_clarification: problems.length ? ['сколько лет в SQL'] : [],
      warnings: [],
      is_current: true,
      status: problems.length ? 'needs_clarification' : 'ready',
      created_at: '2026-10-20T10:00:00Z',
      send_problems: problems,
    },
    response_letter_required: false,
  }
}

const BOARD: BoardView = {
  thresholds: { suitable: 70, maybe: 50 },
  funnel: [
    { key: 'found', label: 'найдено за 30 дней', value: 200, hint: 'без поисков-наблюдений' },
    { key: 'good', label: 'оценка от 50', value: 40, hint: null },
    { key: 'sent', label: 'отправлено', value: 20, hint: null },
    { key: 'viewed', label: 'смотрели', value: 10, hint: null },
    { key: 'invited', label: 'приглашений', value: 1, hint: null },
  ],
  new: { hi: 2, mid: 5, low: 30, wait: 0, manual: 0, fresh_today: 1, clarify: 1, snoozed: 2 },
  columns: [
    { key: 'new', title: 'Новые', count: 7, items: [item(READY), item(MARKED)] },
    { key: 'confirm', title: 'Ждут подтверждения', count: 1, items: [item(row(303, 'Тестировщик ПО'), { application: application(7, { status: 'needs_manual', sent_at: null }) })] },
    { key: 'sent', title: 'Отправлено', count: 9, items: [item(row(404, 'Data engineer'), { application: application(9) })] },
    { key: 'viewed', title: 'Смотрели', count: 0, items: [] },
    { key: 'answer', title: 'Ответ', count: 1, items: [item(row(505, 'Fullstack'), { application: application(11), outcome: 'invitation' })] },
  ],
}

const STATUS = { apply_mode: 'assist', consent_accepted_at: '2026-09-01T10:00:00Z', dry_run: false, limits: { today_sent: 0, max_per_day: 30 } }

function list(key: BoardColumnList['key'], items: BoardItem[], band: BoardColumnList['band'] = null): BoardColumnList {
  return { key, band, total: items.length, offset: 0, items }
}

const API = {
  '/api/board': BOARD,
  '/api/applications/status': STATUS,
  '/api/board/column/new': list('new', [item(READY), item(MARKED)], 'hi'),
  '/api/vacancies/101': detail(READY, 'Здравствуйте! Готовое письмо.'),
  '/api/vacancies/202': detail(MARKED, 'Здравствуйте! [[УТОЧНИТЬ: сколько лет в SQL]] лет работаю с SQL.', ['в письме есть маркеры [[УТОЧНИТЬ]], заполните их']),
  // the page reads nothing from these answers, it refreshes the columns
  '/api/applications/7/confirm': {},
  '/api/applications/9/manual-update': {},
  '/api/applications/assist/open': { ...application(50, { status: 'needs_manual', sent_at: null }), vacancy: { id: 101, hh_id: '1101', name: READY.name, employer_name: 'Компания', alternate_url: null, apply_alternate_url: null, pipeline_status: 'needs_manual', score: 80 }, apply_url: READY.apply_alternate_url },
}

const location = () => screen.getByTestId('location').textContent
const press = (key: string, code = key) => fireEvent.keyDown(window, { key, code })
const focused = () => document.querySelector('[aria-current="true"]')?.getAttribute('data-board-row')

describe('BoardPage', () => {
  it('shows the funnel and the board, «Разобрать новые» opens the best band as a working list', async () => {
    const { calls } = stubApi(API)
    renderAt('/board', '/board', <BoardPage />)

    const funnel = await screen.findByRole('list', { name: 'Воронка за 30 дней' })
    expect(within(funnel).getAllByRole('listitem')).toHaveLength(5)
    // each step with its share of the one before; the weakest transition past the first is tinted
    expect(within(funnel).getByText('20%')).toBeInTheDocument()
    expect(within(funnel).getByLabelText('10% от прошлого шага').closest('[role="listitem"]')).toHaveClass('bg-bad-soft')

    expect(screen.getByRole('heading', { name: 'Новые' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Оценка от 70\s*2/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Письмо ждёт уточнения\s*1/ })).toBeInTheDocument()
    expect(screen.getByText('Отложено: 2')).toBeInTheDocument()
    expect(screen.getByText('Тестировщик ПО')).toBeInTheDocument()
    expect(screen.getByText('Приглашение')).toBeInTheDocument()
    // an empty column has nothing to expand
    expect(screen.queryByRole('button', { name: 'Раскрыть колонку «Смотрели»' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Разобрать новые/ }))
    expect(location()).toBe('/board?col=new&band=hi')
    expect(await screen.findByRole('heading', { name: /^Новые/ })).toHaveAttribute('id', 'board-open-title')
    await waitFor(() => expect(calls).toContain('/api/board/column/new?band=hi&limit=60'))
    // the details of the focused card come from the vacancy itself
    expect(await screen.findByText('Сводка Claude по вакансии Frontend-разработчик')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ещё 2' })).toBeInTheDocument()
  })

  it('walks the cards by keys, opens the response on hh.ru by Enter and holds back a letter with a marker', async () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    const { requests } = stubApi(API)
    renderAt('/board?col=new&band=hi', '/board', <BoardPage />)
    await screen.findByText('Сводка Claude по вакансии Frontend-разработчик')
    expect(focused()).toBe('101')

    press('j', 'KeyJ')
    expect(focused()).toBe('202')
    expect(await screen.findByText('Сначала уточните письмо')).toBeInTheDocument()
    press('Enter')
    expect(await screen.findByText('Отклик пока нельзя открыть: в письме есть маркеры [[УТОЧНИТЬ]], заполните их')).toBeInTheDocument()
    expect(requests.some((r) => r.path === '/api/applications/assist/open')).toBe(false)

    press('k', 'KeyK')
    expect(focused()).toBe('101')
    press('Enter')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/applications/assist/open')?.body).toEqual({ vacancy_id: 101 }))
    expect(await screen.findByRole('region', { name: 'Отклик на hh.ru' })).toBeInTheDocument()
    expect(open).toHaveBeenCalledWith(READY.apply_alternate_url, '_blank', 'noopener')
  })

  it('skips by S, snoozes by D for three days and returns to the board by Esc', async () => {
    const { requests } = stubApi({ ...API, '/api/board/column/new': list('new', [item(READY)], 'mid') })
    renderAt('/board?col=new&band=mid', '/board', <BoardPage />)
    await screen.findByText('Сводка Claude по вакансии Frontend-разработчик')

    press('s', 'KeyS')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/vacancies/101/skip')?.body).toEqual({ reason: null }))
    expect(await screen.findByText('Пропущено')).toBeInTheDocument()
    // the static stub brings the card back with the refetch: then it can be snoozed
    await screen.findByText('Сводка Claude по вакансии Frontend-разработчик')
    press('d', 'KeyD')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/vacancies/101/snooze')?.body).toEqual({ days: 3 }))

    press('Escape')
    expect(location()).toBe('/board')
    expect(await screen.findByRole('list', { name: 'Воронка за 30 дней' })).toBeInTheDocument()
  })

  it('marks an opened response sent by Enter and returns another one by «Не отправил»', async () => {
    const opened = item(row(303, 'Тестировщик ПО'), { application: application(7, { status: 'needs_manual', sent_at: null }) })
    const { requests } = stubApi({ ...API, '/api/board/column/confirm': list('confirm', [opened]) })
    renderAt('/board?col=confirm', '/board', <BoardPage />)
    expect(await screen.findByText(/Письмо скопировалось, когда вы открыли отклик/)).toBeInTheDocument()

    press('Enter')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/applications/7/confirm')?.body).toEqual({ sent: true }))
    expect(await screen.findByText('Отклик отмечен отправленным')).toBeInTheDocument()

    fireEvent.click(await screen.findByRole('button', { name: 'Не отправил' }))
    await waitFor(() => expect(requests.filter((r) => r.path === '/api/applications/7/confirm').map((r) => r.body)).toEqual([{ sent: true }, { sent: false }]))
  })

  it('records the employer side of a sent response by hand: viewed by Enter, an invitation by its button', async () => {
    const sent = item(row(404, 'Data engineer'), { application: application(9) })
    const { requests } = stubApi({ ...API, '/api/board/column/sent': list('sent', [sent]) })
    renderAt('/board?col=sent', '/board', <BoardPage />)
    expect(await screen.findByText(/отправлен 20\.10\.2026/)).toBeInTheDocument()

    press('Enter')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/applications/9/manual-update')?.body).toEqual({ viewed: true }))
    fireEvent.click(await screen.findByRole('button', { name: 'Приглашение' }))
    await waitFor(() => expect(requests.filter((r) => r.path === '/api/applications/9/manual-update').map((r) => r.body)).toEqual([{ viewed: true }, { state: 'invitation' }]))
  })
})
