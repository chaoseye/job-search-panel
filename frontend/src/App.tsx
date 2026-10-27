import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { lazy, Suspense } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import AppShell from './components/AppShell'
import BoardPage from './pages/BoardPage'
import SettingsPage from './pages/SettingsPage'
import LogsPage from './pages/LogsPage'
import ProfilePage from './pages/ProfilePage'
import SearchesPage from './pages/SearchesPage'
import VacanciesPage from './pages/VacanciesPage'
import BatchesPage from './pages/BatchesPage'
import ApplicationsPage from './pages/ApplicationsPage'
import DashboardPage from './pages/DashboardPage'
import AnalyticsPage from './pages/AnalyticsPage'
import ChannelsPage from './pages/ChannelsPage'
import SitesPage from './pages/SitesPage'
import StudiosPage from './pages/StudiosPage'
import TendersPage from './pages/TendersPage'

// the design system page is for whoever changes the interface: its own chunk, loaded only when /ui opens
const DesignSystemPage = lazy(() => import('./pages/DesignSystemPage'))

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 10_000 } },
})

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<DashboardPage />} />
            <Route path="board" element={<BoardPage />} />
            <Route path="vacancies" element={<VacanciesPage />} />
            <Route path="applications" element={<ApplicationsPage />} />
            <Route path="analytics" element={<AnalyticsPage />} />
            <Route path="channels" element={<ChannelsPage />} />
            <Route path="sites" element={<SitesPage />} />
            <Route path="studios" element={<StudiosPage />} />
            <Route path="tenders" element={<TendersPage />} />
            <Route path="batches" element={<BatchesPage />} />
            <Route path="profile" element={<ProfilePage />} />
            <Route path="searches" element={<SearchesPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="logs" element={<LogsPage />} />
            {/* the design system: tokens and components on one page for whoever changes the interface; not in the menu */}
            <Route
              path="ui"
              element={
                <Suspense fallback={null}>
                  <DesignSystemPage />
                </Suspense>
              }
            />
            <Route path="*" element={<Navigate to="/settings" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
