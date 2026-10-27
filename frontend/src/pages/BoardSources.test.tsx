import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type {
  BoardColumnList,
  BoardView,
  StudioBoardItem,
  StudioBoardView,
  StudioDetail,
  StudioLetterView,
  StudioRow,
  TenderBoardItem,
  TenderBoardView,
  TenderDetail,
  TenderRow,
} from '../api/types'
import { renderAt, stubApi } from '../test/render'
import BoardPage from './BoardPage'

function tenderRow(id: number, title: string, over: Partial<TenderRow> = {}): TenderRow {
  return {
    id,
    ws_id: 1000 + id,
    url: `https://workspace.ru/tenders/t-${id}/`,
    title,
    category: 'web-development',
    section: 'Веб-разработка',
    service: null,
    budget: 'до 150 000 ₽',
    budget_max: 150000,
    published_on: '2026-10-20',
    deadline_on: '2099-11-05',
    state: 'open',
    open: true,
    city: 'Москва',
    organizer: null,
    participants: 3,
    views: 10,
    fit_score: 80,
    fit_notes: ['совпадает: Python, Telegram'],
    relevant: true,
    status: 'new',
    status_label: 'новый',
    detail_error: null,
    first_seen_at: '2026-10-24T10:00:00Z',
    responded_at: null,
    answered_at: null,
    notes: null,
    ...over,
  }
}

function tenderDetail(row: TenderRow, reply: string | null = null): TenderDetail {
  return {
    ...row,
    description: 'Нужен сайт для клиники с записью онлайн.',
    conditions: [],
    reply: reply ? { id: 7, version: 1, text: reply, origin: 'claude', batch_id: 'b1', needs_clarification: [], warnings: [], is_current: true, created_at: '2026-10-24T12:00:00Z' } : null,
    replies: [],
    request: null,
  }
}

const FOUND = tenderRow(11, 'Сайт клиники')
const OTHER = tenderRow(13, 'Логотип кафе', { relevant: false, fit_score: 10 })
const READY = tenderRow(22, 'Бот записи', { status: 'drafted', status_label: 'отклик готов' })
const REPLY = 'Здравствуйте! Сделаем бота записи за две недели.'
const RESPONDED = tenderRow(33, 'Сайт для школы', { status: 'responded', status_label: 'откликнулся', responded_at: '2026-10-24T15:00:00Z' })

const tItem = (tender: TenderRow): TenderBoardItem => ({ tender, stage_at: '2026-10-24T10:00:00Z' })

const TENDER_BOARD: TenderBoardView = {
  source: 'tenders',
  funnel: [
    { key: 'found', label: 'найдено за 30 дней', value: 95, hint: null },
    { key: 'suitable', label: 'подходят', value: 23, hint: null },
    { key: 'ready', label: 'отклик готов', value: 4, hint: null },
    { key: 'responded', label: 'откликнулся', value: 1, hint: null },
    { key: 'answered', label: 'ответили', value: 0, hint: null },
  ],
  bands: { relevant: 1, other: 1 },
  fresh_today: 1,
  columns: [
    { key: 'new', title: 'Найдено', count: 1, items: [tItem(FOUND)] },
    { key: 'ready', title: 'Отклик готов', count: 1, items: [tItem(READY)] },
    { key: 'responded', title: 'Откликнулся', count: 1, items: [tItem(RESPONDED)] },
    { key: 'answer', title: 'Ответ', count: 0, items: [] },
  ],
}

function page<I>(key: string, items: I[], band: string | null = null) {
  return { source: 'x', key, band, total: items.length, offset: 0, items }
}

const TENDER_API = {
  '/api/board?source=tenders': TENDER_BOARD,
  '/api/board/column/new': page('new', [tItem(FOUND)], 'relevant'),
  '/api/board/column/new?source=tenders&band=other': page('new', [tItem(OTHER)], 'other'),
  '/api/board/column/ready': page('ready', [tItem(READY)]),
  '/api/tenders/11': tenderDetail(FOUND),
  '/api/tenders/13': tenderDetail(OTHER),
  '/api/tenders/22': tenderDetail(READY, REPLY),
  '/api/tenders/33': tenderDetail(RESPONDED, REPLY),
  '/api/board/column/responded': page('responded', [tItem(RESPONDED)]),
  '/api/tenders/replies': { queued: 1, skipped: [], batch_id: 'b1' },
}

function studioRow(id: number, name: string, over: Partial<StudioRow> = {}): StudioRow {
  return {
    id,
    name,
    site_url: `https://${id}.studio.example`,
    domain: `${id}.studio.example`,
    hh_employer_id: null,
    hh_url: null,
    area: 'Москва',
    kind: 'dev',
    kind_label: 'разработка',
    source: 'list',
    sources: ['list'],
    letter_kind: 'application',
    offer_allowed: true,
    open_vacancies: null,
    services: ['веб-разработка'],
    emails: ['hello@pixel.example'],
    telegram: null,
    contact_url: null,
    contacts_checked_at: '2026-10-20T10:00:00Z',
    contacts_error: null,
    fit_score: 70,
    fit_notes: ['совпадает: Python'],
    status: 'ready',
    status_label: 'есть контакты',
    sent_at: null,
    sent_to: null,
    sent_via: null,
    replied_at: null,
    reply_subject: null,
    first_seen_at: '2026-10-20T10:00:00Z',
    notes: null,
    ...over,
  }
}

function letter(id: number, style: 'business' | 'simple', text: string, current: boolean): StudioLetterView {
  return {
    id,
    version: id,
    kind: 'application',
    style,
    style_label: style === 'business' ? 'деловой' : 'простой',
    subject: 'Разработчик для ваших проектов',
    text,
    full_text: `${text}\n\nИван Петров`,
    origin: 'claude',
    batch_id: 'b1',
    needs_clarification: [],
    warnings: [],
    is_current: current,
    created_at: '2026-10-24T12:00:00Z',
  }
}

function studioDetail(row: StudioRow, letters: StudioLetterView[] = []): StudioDetail {
  return { ...row, description: null, site_summary: 'Делаем сайты и сервисы', letter: letters.find((l) => l.is_current) ?? null, variants: letters, letters, request: null, vacancies: [] }
}

const WITH_CONTACTS = studioRow(31, 'Пиксель')
const UNCHECKED = studioRow(32, 'Квадрат', { status: 'new', emails: [], contacts_checked_at: null })
const TG_ONLY = studioRow(41, 'Код', { status: 'drafted', status_label: 'письмо готово', emails: [], telegram: '@example_kod_studio' })
const MAILED = studioRow(42, 'Вектор', { status: 'drafted', status_label: 'письмо готово' })
const SENT = studioRow(51, 'Линия', { status: 'sent', sent_at: '2026-10-23T10:00:00Z', sent_via: 'email', sent_to: 'hello@pixel.example' })
const BUSINESS = letter(61, 'business', 'Здравствуйте! Пишу backend для веб-проектов.', true)
const SIMPLE = letter(62, 'simple', 'Привет! Делаю ботов и сайты.', false)

const sItem = (studio: StudioRow): StudioBoardItem => ({ studio, stage_at: '2026-10-24T10:00:00Z' })

const STUDIO_BOARD: StudioBoardView = {
  source: 'studios',
  funnel: [
    { key: 'work', label: 'студий в работе', value: 327, hint: null },
    { key: 'contacts', label: 'есть контакты', value: 294, hint: null },
    { key: 'letter', label: 'письмо готово', value: 42, hint: null },
    { key: 'sent', label: 'отправлено', value: 1, hint: null },
    { key: 'answered', label: 'ответили', value: 0, hint: null },
  ],
  bands: { contacts: 1, unchecked: 1, nocontacts: 3 },
  sent_today: 1,
  daily_limit: 20,
  columns: [
    { key: 'new', title: 'Найдено', count: 2, items: [sItem(WITH_CONTACTS)] },
    { key: 'ready', title: 'Письмо готово', count: 2, items: [sItem(TG_ONLY), sItem(MAILED)] },
    { key: 'sent', title: 'Отправлено', count: 1, items: [sItem(SENT)] },
    { key: 'answer', title: 'Ответили', count: 0, items: [] },
  ],
}

const STUDIO_API = {
  '/api/board?source=studios': STUDIO_BOARD,
  '/api/board/column/ready': page('ready', [sItem(TG_ONLY), sItem(MAILED)]),
  '/api/board/column/sent': page('sent', [sItem(SENT)]),
  '/api/studios/31': studioDetail(WITH_CONTACTS),
  '/api/studios/32': studioDetail(UNCHECKED),
  '/api/studios/41': studioDetail(TG_ONLY, [BUSINESS, SIMPLE]),
  '/api/studios/42': studioDetail(MAILED, [BUSINESS]),
  '/api/studios/51': studioDetail(SENT, [BUSINESS]),
  '/api/studios/letters': { queued: 1, skipped: [], batch_id: null },
  '/api/studios/contacts': { job_run_id: 5 },
}

const location = () => screen.getByTestId('location').textContent
const press = (key: string, code = key) => fireEvent.keyDown(window, { key, code })
const focused = () => document.querySelector('[aria-current="true"]')?.getAttribute('data-board-row')

// jsdom has no clipboard: a stub the tests read back, removed after each test
function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  return writeText
}

afterEach(() => {
  Reflect.deleteProperty(navigator, 'clipboard')
})

describe('BoardPage: tenders', () => {
  it('shows the tender funnel and summary, opens the suitable ones, asks Claude for an answer by Enter and skips by S', async () => {
    const { calls, requests } = stubApi(TENDER_API)
    renderAt('/board?src=tenders', '/board', <BoardPage />)

    const funnel = await screen.findByRole('list', { name: 'Воронка тендеров за 30 дней' })
    expect(within(funnel).getAllByRole('listitem')).toHaveLength(5)
    expect(screen.getByRole('radio', { name: 'Тендеры' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('1 подходящий тендер открыт · откликов в работе: 1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Подходят\s*1/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Остальные открытые\s*1/ })).toBeInTheDocument()
    expect(screen.getByText('Сегодня новых: 1')).toBeInTheDocument()
    expect(screen.getByText('Бот записи')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Разобрать найденные/ }))
    expect(location()).toBe('/board?src=tenders&col=new&band=relevant')
    await waitFor(() => expect(calls).toContain('/api/board/column/new?source=tenders&band=relevant&limit=60'))
    expect(await screen.findByText('Нужен сайт для клиники с записью онлайн.')).toBeInTheDocument()
    expect(focused()).toBe('11')

    press('Enter')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/tenders/replies')?.body).toEqual({ ids: [11] }))
    expect(await screen.findByText('Отклик в очереди для Claude, пакет b1')).toBeInTheDocument()
    // the static stub brings the card back with the refetch: then it can be skipped
    await screen.findByText('Нужен сайт для клиники с записью онлайн.')
    press('s', 'KeyS')
    await waitFor(() => expect(requests.some((r) => r.path === '/api/tenders/11/skip' && r.method === 'POST')).toBe(true))

    // the other open tenders are a band of their own
    fireEvent.click(screen.getByRole('radio', { name: 'Остальные открытые · 1' }))
    expect(location()).toBe('/board?src=tenders&col=new&band=other')
    expect(await screen.findByText('Логотип кафе')).toBeInTheDocument()
    expect(calls).toContain('/api/board/column/new?source=tenders&band=other&limit=60')
  })

  it('opens an answer on workspace.ru with the text copied, then «Да, откликнулся» marks it posted', async () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    const writeText = stubClipboard()
    const { requests } = stubApi(TENDER_API)
    renderAt('/board?src=tenders&col=ready', '/board', <BoardPage />)
    expect(await screen.findByText(REPLY)).toBeInTheDocument()

    press('Enter')
    await waitFor(() => expect(open).toHaveBeenCalledWith(READY.url, '_blank', 'noopener'))
    expect(writeText).toHaveBeenCalledWith(REPLY)
    expect(await screen.findByRole('button', { name: /Да, откликнулся/ })).toBeInTheDocument()
    expect(requests.some((r) => r.path.endsWith('/responded'))).toBe(false)

    press('Enter')
    await waitFor(() => expect(requests.some((r) => r.path === '/api/tenders/22/responded' && r.method === 'POST')).toBe(true))
  })
})

describe('BoardPage: tender answers', () => {
  it('records the organizer\'s answer of a responded tender: «Ответили» by Enter, a refusal by its button', async () => {
    const { requests } = stubApi(TENDER_API)
    renderAt('/board?src=tenders&col=responded', '/board', <BoardPage />)
    expect(await screen.findByText(/откликнулся 24\.10\.2026/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Ответили/ })).toBeInTheDocument()
    press('Enter')
    await waitFor(() => expect(requests.some((r) => r.path === '/api/tenders/33/replied' && r.method === 'POST')).toBe(true))
    expect(await screen.findByText('Записано: заказчик ответил')).toBeInTheDocument()
    // the static stub brings the card back with the refetch: a refusal by its own button
    fireEvent.click(await screen.findByRole('button', { name: 'Отказ' }))
    await waitFor(() => expect(requests.some((r) => r.path === '/api/tenders/33/declined' && r.method === 'POST')).toBe(true))
  })
})

describe('BoardPage: studios', () => {
  it('asks Claude for a letter by Enter and checks the contacts of a studio whose site was not read', async () => {
    const { requests } = stubApi({
      ...STUDIO_API,
      '/api/board/column/new': page('new', [sItem(WITH_CONTACTS)], 'contacts'),
    })
    renderAt('/board?src=studios&col=new&band=contacts', '/board', <BoardPage />)
    expect(await screen.findByText('Делаем сайты и сервисы')).toBeInTheDocument()
    press('Enter')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/studios/letters')?.body).toEqual({ ids: [31] }))
    expect(await screen.findByText('Письмо в очереди для Claude')).toBeInTheDocument()
  })

  it('a studio not checked yet: Enter starts reading its site and the card stays', async () => {
    const { requests } = stubApi({ ...STUDIO_API, '/api/board/column/new': page('new', [sItem(UNCHECKED)], 'unchecked') })
    renderAt('/board?src=studios&col=new&band=unchecked', '/board', <BoardPage />)
    expect(await screen.findByRole('button', { name: /Проверить контакты/ })).toBeInTheDocument()
    press('Enter')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/studios/contacts')?.body).toEqual({ ids: [32] }))
    expect(await screen.findByText(/Панель читает сайт студии/)).toBeInTheDocument()
    expect(focused()).toBe('32')
  })

  it('opens a letter in Telegram with the picked draft copied, then «Да, отправлено» marks that draft sent', async () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    const writeText = stubClipboard()
    const { requests } = stubApi(STUDIO_API)
    renderAt('/board?src=studios&col=ready', '/board', <BoardPage />)
    expect(await screen.findByText(/Пишу backend для веб-проектов/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: 'Простой' }))
    expect(await screen.findByText(/Делаю ботов и сайты/)).toBeInTheDocument()
    press('Enter')
    await waitFor(() => expect(open).toHaveBeenCalledWith('https://t.me/example_kod_studio', '_blank', 'noopener'))
    expect(writeText).toHaveBeenCalledWith(SIMPLE.full_text)
    expect(await screen.findByRole('button', { name: /Да, отправлено/ })).toBeInTheDocument()

    press('Enter')
    await waitFor(() => expect(requests.find((r) => r.path === '/api/studios/41/sent')?.body).toEqual({ to: '@example_kod_studio', via: 'telegram', letter_id: 62 }))
  })

  it('marks a letter sent by e-mail right away and records the answer of a sent one', async () => {
    const { requests } = stubApi(STUDIO_API)
    renderAt('/board?src=studios&col=ready', '/board', <BoardPage />)
    await screen.findByText(/Пишу backend для веб-проектов/)
    press('j', 'KeyJ')
    expect(focused()).toBe('42')
    const already = await screen.findByRole('button', { name: 'Уже отправил' })
    await waitFor(() => expect(already).toBeEnabled()) // the letter of the card has loaded
    fireEvent.click(already)
    await waitFor(() => expect(requests.find((r) => r.path === '/api/studios/42/sent')?.body).toEqual({ to: 'hello@pixel.example', via: 'email', letter_id: 61 }))

    press('ArrowRight')
    expect(location()).toBe('/board?src=studios&col=sent')
    await screen.findByRole('button', { name: /Ответили/ })
    press('Enter')
    await waitFor(() => expect(requests.some((r) => r.path === '/api/studios/51/replied' && r.method === 'POST')).toBe(true))
  })
})

describe('BoardPage: the source switch', () => {
  it('switches the board from vacancies to studios and back', async () => {
    const vacancies: BoardView = {
      thresholds: { suitable: 70, maybe: 50 },
      funnel: [{ key: 'found', label: 'найдено за 30 дней', value: 10, hint: null }],
      new: { hi: 0, mid: 0, low: 0, wait: 0, manual: 0, fresh_today: 0, clarify: 0, snoozed: 0 },
      columns: [
        { key: 'new', title: 'Новые', count: 0, items: [] },
        { key: 'confirm', title: 'Ждут подтверждения', count: 0, items: [] },
      ],
    }
    const empty: BoardColumnList = { key: 'new', band: 'hi', total: 0, offset: 0, items: [] }
    const { calls } = stubApi({ ...STUDIO_API, '/api/board': vacancies, '/api/board/column/new': empty, '/api/applications/status': { apply_mode: 'assist', consent_accepted_at: null, dry_run: false } })
    renderAt('/board', '/board', <BoardPage />)
    expect(await screen.findByRole('list', { name: 'Воронка за 30 дней' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: 'Студии' }))
    expect(location()).toBe('/board?src=studios')
    expect(await screen.findByRole('list', { name: 'Воронка студий' })).toBeInTheDocument()
    expect(calls).toContain('/api/board?source=studios')
    expect(screen.getByText('1 студия с контактами ждёт письма · писем в работе: 2 · отправлено сегодня 1 из 20')).toBeInTheDocument()
    expect(screen.getByText('Отправлено сегодня: 1 из 20')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: 'Вакансии' }))
    expect(location()).toBe('/board')
  })
})
