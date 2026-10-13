import { useEffect, useState } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { api, post, setUnauthorizedHandler } from './api'
import { Layout } from './components/Layout'
import { Spinner } from './components/ui'
import Agents from './pages/Agents'
import Backtests from './pages/Backtests'
import Historical from './pages/Historical'
import Login from './pages/Login'
import Overview from './pages/Overview'
import Paper from './pages/Paper'
import Portfolio from './pages/Portfolio'
import Reports from './pages/Reports'
import Scanner from './pages/Scanner'
import SettingsPage from './pages/Settings'
import Strategies from './pages/Strategies'

export default function App() {
  const [user, setUser] = useState<string | null | undefined>(undefined)

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null))
    api<{ username: string }>('/auth/me').then((r) => setUser(r.username)).catch(() => setUser(null))
  }, [])

  if (user === undefined) return <Spinner label="Connecting to Arbies…" />
  if (user === null) return <Login onLogin={setUser} />

  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout user={user} onLogout={async () => { await post('/auth/logout'); setUser(null) }} />}>
          <Route index element={<Overview />} />
          <Route path="scanner" element={<Scanner />} />
          <Route path="backtests" element={<Backtests />} />
          <Route path="historical" element={<Historical />} />
          <Route path="paper" element={<Paper />} />
          <Route path="agents" element={<Agents />} />
          <Route path="strategies" element={<Strategies />} />
          <Route path="portfolio" element={<Portfolio />} />
          <Route path="reports" element={<Reports />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
