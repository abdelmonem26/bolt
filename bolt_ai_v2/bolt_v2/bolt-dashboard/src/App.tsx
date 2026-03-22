import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Sidebar from './components/Sidebar'
import Header from './components/Header'
import { ErrorBoundary } from './components/ErrorBoundary'
import Dashboard from './pages/Dashboard'
import ContentManagement from './pages/ContentManagement'
import Analytics from './pages/Analytics'
import NewsMonitor from './pages/NewsMonitor'
import PlatformManagement from './pages/PlatformManagement'
import CostBackups from './pages/CostBackups'
import Settings from './pages/Settings'
import Login from './pages/Login'

const queryClient = new QueryClient({ defaultOptions: { queries: { refetchInterval: 30000, retry: 1 } } })

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Router>
        <Routes>
          {/* Login page -- no sidebar/header */}
          <Route path="/login" element={<Login />} />

          {/* Main dashboard layout */}
          <Route path="/*" element={
            <div style={{ display: 'flex', minHeight: '100vh', background: 'var(--bolt-dark)' }}>
              <Sidebar />
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                <Header />
                <main style={{ flex: 1, overflowY: 'auto', padding: 24 }}>
                  <ErrorBoundary>
                    <Routes>
                      <Route path="/"          element={<Dashboard />} />
                      <Route path="/content"   element={<ContentManagement />} />
                      <Route path="/analytics" element={<Analytics />} />
                      <Route path="/news"      element={<NewsMonitor />} />
                      <Route path="/platforms" element={<PlatformManagement />} />
                      <Route path="/costs"     element={<CostBackups />} />
                      <Route path="/settings"  element={<Settings />} />
                    </Routes>
                  </ErrorBoundary>
                </main>
              </div>
            </div>
          } />
        </Routes>
      </Router>
    </QueryClientProvider>
  )
}
