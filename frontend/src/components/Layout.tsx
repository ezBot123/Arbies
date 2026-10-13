import clsx from 'clsx'
import {
  Activity, BarChart3, Bot, Briefcase, FileText, FlaskConical, LayoutDashboard, LogOut, Moon, Octagon, Radar,
  Settings, SlidersHorizontal, Sun,
} from 'lucide-react'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { post, type Tick } from '../api'
import { useStream } from '../hooks'
import { Badge, Button, SourceBadge, useConfirm } from './ui'
import { useQueryClient } from '@tanstack/react-query'

const StreamCtx = createContext<{ tick: Tick | null; connected: boolean }>({ tick: null, connected: false })
export const useTick = () => useContext(StreamCtx)

const NAV = [
  { to: '/', label: 'Overview', icon: LayoutDashboard },
  { to: '/scanner', label: 'Opportunity Scanner', icon: Radar },
  { to: '/backtests', label: 'Backtesting Lab', icon: FlaskConical },
  { to: '/paper', label: 'Paper Trading', icon: Activity },
  { to: '/agents', label: 'AI Agent Centre', icon: Bot },
  { to: '/strategies', label: 'Strategy Manager', icon: SlidersHorizontal },
  { to: '/portfolio', label: 'Portfolio', icon: Briefcase },
  { to: '/reports', label: 'Reports', icon: FileText },
  { to: '/settings', label: 'System Settings', icon: Settings },
]

function useTheme() {
  const [theme, setTheme] = useState<string>(() => localStorage.getItem('arbies-theme') ?? 'dark')
  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('arbies-theme', theme)
  }, [theme])
  return { theme, toggle: () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')) }
}

export function Layout({ user, onLogout }: { user: string; onLogout: () => void }) {
  const stream = useStream()
  const { theme, toggle } = useTheme()
  const { ask, dialog } = useConfirm()
  const qc = useQueryClient()
  const tick = stream.tick
  const live = tick?.live_armed
  const kill = tick?.kill_switch

  const activateKill = () =>
    ask({
      title: 'Activate global kill switch?',
      message: 'All new simulated and live order flow stops immediately and live trading is disarmed. Releasing it later requires your password.',
      danger: true,
      label: 'Activate kill switch',
      action: async () => { await post('/risk/kill-switch', { active: true, reason: 'manual (dashboard)' }); qc.invalidateQueries() },
    })

  return (
    <StreamCtx.Provider value={stream}>
      <div className="flex h-full">
        <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-panel">
          <div className="flex items-center gap-2 px-4 py-3.5">
            <BarChart3 size={20} className="text-accent" />
            <span className="text-[16px] font-bold tracking-tight">Arbies</span>
            <span className="text-[10px] text-muted">research</span>
          </div>
          <nav className="flex-1 space-y-0.5 px-2">
            {NAV.map(({ to, label, icon: Icon }) => (
              <NavLink key={to} to={to} end={to === '/'}
                className={({ isActive }) => clsx('flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px]',
                  isActive ? 'bg-panel2 font-medium text-fg' : 'text-muted hover:bg-panel2/60 hover:text-fg')}>
                <Icon size={15} /> {label}
              </NavLink>
            ))}
          </nav>
          <div className="space-y-2 border-t border-line p-3 text-[11px] text-muted">
            <div className="flex items-center justify-between">
              <span>Stream</span>
              <span className={stream.connected ? 'text-pos' : 'text-neg'}>{stream.connected ? 'connected' : 'offline'}</span>
            </div>
            <div className="flex items-center justify-between">
              <span>{user}</span>
              <div className="flex gap-1">
                <button title="Toggle theme" onClick={toggle} className="rounded p-1 hover:bg-panel2">
                  {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
                </button>
                <button title="Log out" onClick={onLogout} className="rounded p-1 hover:bg-panel2"><LogOut size={14} /></button>
              </div>
            </div>
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className={clsx('flex items-center gap-3 border-b px-4 py-2', live ? 'border-live bg-live/10' : 'border-line bg-panel')}>
            {live ? (
              <Badge tone="live" title="Real money mode is armed">LIVE TRADING ARMED</Badge>
            ) : (
              <Badge tone="sim" title="No real orders are placed in paper mode">PAPER MODE · SIMULATED</Badge>
            )}
            <SourceBadge source={tick?.data_source} />
            {kill && <Badge tone="neg">KILL SWITCH ACTIVE</Badge>}
            <div className="ml-2 flex flex-wrap gap-1.5">
              {tick && Object.entries(tick.exchanges).map(([ex, h]) => (
                <span key={ex} title={`${h.status} via ${h.transport}; ${h.updates} updates, ${h.errors} errors`}
                      className="inline-flex items-center gap-1 rounded border border-line px-1.5 py-0.5 text-[11px]">
                  <span className={clsx('h-1.5 w-1.5 rounded-full', h.healthy ? 'bg-pos' : 'bg-neg')} /> {ex}
                </span>
              ))}
            </div>
            <div className="ml-auto">
              {!kill && (
                <Button variant="danger" size="sm" onClick={activateKill} title="Stop all order flow">
                  <Octagon size={13} /> Kill switch
                </Button>
              )}
            </div>
          </header>
          <main className="min-h-0 flex-1 overflow-auto p-4">
            <Outlet />
          </main>
        </div>
      </div>
      {dialog}
    </StreamCtx.Provider>
  )
}

export function PageTitle({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-2">
      <div>
        <h1 className="text-[18px] font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-0.5 text-[12px] text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  )
}
