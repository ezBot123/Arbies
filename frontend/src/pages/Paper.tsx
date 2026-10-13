import { useQueryClient } from '@tanstack/react-query'
import { Pause, Play, RotateCcw, Square } from 'lucide-react'
import { useState } from 'react'
import { post } from '../api'
import { EquityChart } from '../components/charts'
import { PageTitle } from '../components/Layout'
import { Badge, Button, Card, Empty, ErrorBox, Field, KV, Notice, SourceBadge, Spinner, Stat, useConfirm } from '../components/ui'
import { useApi } from '../hooks'
import { gbp, label, num, signClass } from '../lib/format'
import { TradesTable } from './Backtests'

export default function Paper() {
  const qc = useQueryClient()
  const st = useApi<any>('/paper', 2000)
  const trades = useApi<any[]>('/paper/trades?limit=300', 4000)
  const equity = useApi<any[]>('/paper/equity', 10000)
  const [error, setError] = useState<unknown>(null)
  const [cfg, setCfg] = useState({ capital: '500', strategies: ['cross_exchange', 'triangular'], min: '0.30', max: '25', latency: '250' })
  const { ask, dialog } = useConfirm()
  const s = st.data
  if (!s) return <Spinner />

  const act = async (path: string, body?: any) => {
    setError(null)
    try { await post(`/paper/${path}`, body); qc.invalidateQueries() } catch (e) { setError(e) }
  }
  const start = () => {
    if (s.status === 'paused') return act('start')
    const d = s.config
    act('start', { ...d, initial_capital_gbp: cfg.capital, strategies: cfg.strategies,
      params: { ...d.params, min_net_profit_pct: cfg.min, max_trade_gbp: cfg.max, latency_ms: Number(cfg.latency) },
      fill: { ...d.fill, latency_ms: Number(cfg.latency) },
      risk: { ...d.risk, max_position_gbp: cfg.max, min_net_return_pct: cfg.min } })
  }
  const curve = (equity.data ?? []).map((p) => ({ ts: p.ts, value: Number(p.value_gbp) }))
  return (
    <div className="space-y-4">
      <PageTitle
        title="Paper Trading"
        subtitle="Forward testing on current market data with virtual balances. Uses the same detection, risk and fill logic as the backtester. No exchange orders are placed."
        actions={<><Badge tone="sim">SIMULATED</Badge><SourceBadge source={s.data_source} /></>}
      />
      {s.data_source === 'synthetic' && <Notice>Paper trading is running on SYNTHETIC market data — results are not meaningful for real markets.</Notice>}
      <Card bodyClass="flex flex-wrap items-end gap-3">
        <div className="flex items-center gap-2">
          <span className="text-[12px] text-muted">Status</span>
          <Badge tone={s.status === 'running' ? 'pos' : s.status === 'paused' ? 'warn' : 'muted'}>{s.status}</Badge>
          {s.session_id && <span className="text-[11px] text-muted">session #{s.session_id}</span>}
        </div>
        {s.status === 'stopped' && (
          <>
            <Field label="Virtual capital (£)"><input className="w-24" value={cfg.capital} onChange={(e) => setCfg({ ...cfg, capital: e.target.value })} /></Field>
            <Field label="Min net %"><input className="w-16" value={cfg.min} onChange={(e) => setCfg({ ...cfg, min: e.target.value })} /></Field>
            <Field label="Max trade £"><input className="w-16" value={cfg.max} onChange={(e) => setCfg({ ...cfg, max: e.target.value })} /></Field>
            <Field label="Latency ms"><input className="w-16" value={cfg.latency} onChange={(e) => setCfg({ ...cfg, latency: e.target.value })} /></Field>
            <div className="flex gap-2 pb-1 text-[12px]">
              {['cross_exchange', 'triangular'].map((k) => (
                <label key={k} className="flex items-center gap-1"><input type="checkbox" checked={cfg.strategies.includes(k)}
                  onChange={(e) => setCfg({ ...cfg, strategies: e.target.checked ? [...cfg.strategies, k] : cfg.strategies.filter((x) => x !== k) })} />{label(k)}</label>
              ))}
            </div>
          </>
        )}
        <div className="ml-auto flex gap-2">
          {s.status !== 'running' && <Button variant="success" onClick={start}><Play size={14} /> {s.status === 'paused' ? 'Resume' : 'Start'}</Button>}
          {s.status === 'running' && <Button onClick={() => act('pause')}><Pause size={14} /> Pause</Button>}
          {s.status !== 'stopped' && <Button variant="danger" onClick={() => ask({ title: 'Stop paper session?', message: 'The session ends; balances and trades are kept for review. A new session starts fresh.', label: 'Stop', danger: true, action: () => act('stop') })}><Square size={14} /> Stop</Button>}
          <Button variant="ghost" onClick={() => ask({ title: 'Reset paper trading?', message: 'Stops the session and clears the virtual portfolio. Historical sessions remain in the database.', label: 'Reset', danger: true, action: () => act('reset') })}><RotateCcw size={14} /> Reset</Button>
        </div>
      </Card>
      <ErrorBox error={error} />
      {s.last_error && <ErrorBox error={`Last loop error: ${s.last_error}`} />}
      {!s.balances ? <Card><Empty>No active session. Configure and start a paper session above.</Empty></Card> : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
            <Stat label="Portfolio value" value={gbp(s.value_gbp)} sub={s.valuation_estimated ? 'uses fallback FX' : 'at mid prices'} />
            <Stat label="Total change" value={gbp(s.total_pnl_gbp)} tone={signClass(s.total_pnl_gbp)} sub="simulated, incl. inventory moves" />
            <Stat label="Trade P&L (session)" value={gbp(s.persisted?.simulated_realised_pnl_gbp)} tone={signClass(s.persisted?.simulated_realised_pnl_gbp)} sub={`${s.persisted?.trades ?? 0} trades`} tip="realised_trade_pnl" />
            <Stat label="Daily trade P&L" value={gbp(s.daily_realised_pnl_gbp)} tone={signClass(s.daily_realised_pnl_gbp)} />
            <Stat label="Fees paid" value={gbp(s.persisted?.fees_gbp)} />
            <Stat label="Crypto exposure" value={gbp(s.exposure?.total_gbp)} />
          </div>
          <div className="grid gap-4 xl:grid-cols-3">
            <Card title="Equity curve (simulated)" className="xl:col-span-2">{curve.length > 1 ? <EquityChart series={[{ name: 'paper', points: curve }]} /> : <Empty>Equity points are recorded every 10 seconds.</Empty>}</Card>
            <Card title="Counters">
              <KV rows={[
                ['Evaluations', s.counters?.evaluations], ['Detected episodes', s.counters?.detected], ['Eligible', s.counters?.eligible],
                ['Attempted', s.counters?.attempted], ['Active executions', s.active_jobs?.length ?? 0],
                ['Kill switch', s.risk?.kill_switch ? 'ACTIVE' : 'off'], ['Circuit breaker', s.risk?.circuit_breaker_active ? 'ACTIVE' : 'off'],
              ]} />
              <div className="mt-3 text-[11px] uppercase text-muted">Risk rejections</div>
              {Object.entries(s.risk_rejections ?? {}).map(([k, v]) => <div key={k} className="flex justify-between text-[12px]"><span>{label(k)}</span><span className="num">{String(v)}</span></div>)}
            </Card>
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <Card title="Virtual balances" bodyClass="p-0">
              <table className="data">
                <thead><tr><th>Exchange</th><th>Asset</th><th className="r">Amount</th><th className="r">Value</th></tr></thead>
                <tbody>{s.balances.map((b: any) => <tr key={b.exchange + b.asset}><td>{b.exchange}</td><td>{b.asset}</td><td className="r num">{num(b.amount, 8)}</td><td className="r num">{gbp(b.value)}</td></tr>)}</tbody>
              </table>
            </Card>
            <Card title="Active exposure" bodyClass="space-y-2">
              <div className="text-[11px] uppercase text-muted">By asset</div>
              {Object.entries(s.exposure?.by_asset ?? {}).map(([k, v]) => <div key={k} className="flex justify-between"><span>{k}</span><span className="num">{gbp(v as string)}</span></div>)}
              <div className="pt-2 text-[11px] uppercase text-muted">By exchange</div>
              {Object.entries(s.exposure?.by_exchange ?? {}).map(([k, v]) => <div key={k} className="flex justify-between"><span>{k}</span><span className="num">{gbp(v as string)}</span></div>)}
            </Card>
          </div>
        </>
      )}
      <Card title="Simulated order ledger" bodyClass="p-0 max-h-[50vh] overflow-auto" actions={<a className="text-[12px] text-accent" href="/api/reports/csv?source=paper">Export CSV</a>}>
        {!trades.data?.length ? <Empty>No simulated trades yet.</Empty> : <TradesTable trades={trades.data} />}
      </Card>
      {dialog}
    </div>
  )
}
