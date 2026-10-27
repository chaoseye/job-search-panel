import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { vi } from 'vitest'

/** fetch stub: the first route whose prefix matches the requested path answers with its JSON; unknown paths get
 *  404. Every requested path is recorded in `calls`, with its method and JSON body in `requests`. */
export function stubApi(routes: Record<string, unknown>): {
  calls: string[]
  requests: { path: string; method: string; body: unknown }[]
} {
  const calls: string[] = []
  const requests: { path: string; method: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname + input.search : input.url
      calls.push(path)
      requests.push({
        path,
        method: (init?.method ?? 'GET').toUpperCase(),
        body: typeof init?.body === 'string' ? JSON.parse(init.body) : null,
      })
      const key = Object.keys(routes)
        .sort((a, b) => b.length - a.length)
        .find((prefix) => path.startsWith(prefix))
      if (key === undefined) return new Response(JSON.stringify({ detail: `no stub for ${path}` }), { status: 404 })
      return new Response(JSON.stringify(routes[key]), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }),
  )
  return { calls, requests }
}

/** The current address (path, query and hash) as text, for tests that follow a link. */
export function Where() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname + location.search + location.hash}</div>
}

/** Renders a page at `url` inside a fresh query client and a memory router; `location` shows where links lead. */
export function renderAt(url: string, path: string, element: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path={path} element={element} />
          <Route path="*" element={null} />
        </Routes>
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}
