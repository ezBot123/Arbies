import { useState } from 'react'
import { PnlBars } from '../components/charts'
import { PageTitle } from '../components/Layout'
import { Badge, Card, Empty, Field, Spinner } from '../components/ui'
import { useApi } from '../hooks'
import { gbp, label, signClass } from '../lib/format'

export default function Reports() {
  const [period, setPeriod] = useState('daily')
  const [source, setSource] = useState('paper')
  const [runId, setRunId] = useState<string>('')
  const [sessionId, setSessionId] = useState<string>('')
  const runs = useApi<any[]>('/backtests')
  const sessions = useApi<any[]>('/paper/sessions')
  const q = source === 'backtest' ? (runId ? `&run_id=${runId}` : null) : (sessionId ? `&session_id=${sessionId}` : '')
  const report = useApi<any>(q === null ? null : `/reports/summary?period=${period}&source=${source}${q}`, 15000)
  const csvHref = `/api/reports/csv?source=${source}${q ?? ''}`
  return (
    <div className="space-y-4">
      <PageTitle title="Reports" subtitle="Performance summaries from simulated trades (paper sessions or backtests). Net of fees and hedge losses." actions={<Badge tone="sim">SIMULATED</Badge>} />
      <Card bodyClass="flex flex-wrap items-end gap-3">
        <Field label="Source"><select value={source} onChange={(e) => setSource(e.target.value)}><option value="paper">Paper trading</option><option value="backtest">Backtest</option></select></Field>
        {source === 'paper' ? (
          <Field label="Session"><select value={sessionId} onChange={(e) => setSessionId(e.target.value)}><option value="">current</option>{(sessions.data ?? []).map((s) => <option key={s.id} value={s.id}>#{s.id} {s.name} ({s.status})</option>)}</select></Field>
        ) : (
          <Field label="Backtest run"><select value={runId} onChange={(e) => setRunId(e.target.value)}><option value="">select…</option>{(runs.data ?? []).filter((r) => r.status === 'completed').map((r) => <option key={r.id} value={r.id}>#{r.id} {r.name}</option>)}</select></Field>
        )}
        <Field label="Period"><select value={period} onChange={(e) => setPeriod(e.target.value)}><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option></select></Field>
        <a className="ml-auto text-[12px] text-accent" href={csvHref}>Export trades CSV</a>
      </Card>
      {q === null ? <Card><Empty>Select a completed backtest run.</Empty></Card> : !report.data ? <Spinner /> : (
        <>
          <Card title={`${label(period)} performance`} bodyClass="p-0">
            {report.data.periods.length === 0 ? <Empty>No trades in this source yet.</Empty> : (
              <table className="data">
                <thead><tr><th>Period</th><th className="r">Trades</th><th className="r">Successful</th><th className="r">Notional</th><th className="r">Est. net (pre-trade)</th><th className="r">Simulated P&amp;L</th><th className="r">Fees</th><th className="r">Hedge losses</th></tr></thead>
                <tbody>{report.data.periods.map((p: any) => (
                  <tr key={p.period}><td>{p.period}</td><td className="r num">{p.trades}</td><td className="r num">{p.successful}</td><td className="r num">{gbp(p.notional_gbp)}</td><td className="r num">{gbp(p.expected_net_gbp)}</td>
                    <td className={`r num ${signClass(p.pnl_gbp)}`}>{gbp(p.pnl_gbp)}</td><td className="r num">{gbp(p.fees_gbp)}</td><td className="r num">{gbp(p.hedge_losses_gbp)}</td></tr>
                ))}</tbody>
              </table>
            )}
          </Card>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Strategy comparison">
              <PnlBars data={Object.entries(report.data.by_strategy).map(([k, v]: any) => ({ name: label(k), value: Number(v.pnl_gbp) }))} />
              <table className="data mt-2"><thead><tr><th>Strategy</th><th className="r">Trades</th><th className="r">P&amp;L</th><th className="r">Fees</th></tr></thead>
                <tbody>{Object.entries(report.data.by_strategy).map(([k, v]: any) => <tr key={k}><td>{label(k)}</td><td className="r num">{v.trades}</td><td className={`r num ${signClass(v.pnl_gbp)}`}>{gbp(v.pnl_gbp)}</td><td className="r num">{gbp(v.fees_gbp)}</td></tr>)}</tbody></table>
            </Card>
            <Card title="Exchange comparison">
              <PnlBars data={Object.entries(report.data.by_exchange).map(([k, v]: any) => ({ name: k, value: Number(v.pnl_gbp) }))} />
              <table className="data mt-2"><thead><tr><th>Exchanges</th><th className="r">Trades</th><th className="r">P&amp;L</th><th className="r">Fees</th></tr></thead>
                <tbody>{Object.entries(report.data.by_exchange).map(([k, v]: any) => <tr key={k}><td>{k}</td><td className="r num">{v.trades}</td><td className={`r num ${signClass(v.pnl_gbp)}`}>{gbp(v.pnl_gbp)}</td><td className="r num">{gbp(v.fees_gbp)}</td></tr>)}</tbody></table>
            </Card>
          </div>
          <Card title="Trading costs">
            {(() => {
              const fees = report.data.periods.reduce((a: number, p: any) => a + Number(p.fees_gbp), 0)
              const hedge = report.data.periods.reduce((a: number, p: any) => a + Number(p.hedge_losses_gbp), 0)
              const pnl = report.data.periods.reduce((a: number, p: any) => a + Number(p.pnl_gbp), 0)
              const est = report.data.periods.reduce((a: number, p: any) => a + Number(p.expected_net_gbp), 0)
              return (
                <div className="grid grid-cols-2 gap-3 text-[12px] md:grid-cols-4">
                  <div><div className="text-muted">Fees paid</div><div className="num text-[16px]">{gbp(fees)}</div></div>
                  <div><div className="text-muted">Hedge losses</div><div className="num text-[16px]">{gbp(hedge)}</div></div>
                  <div><div className="text-muted">Estimated net before execution</div><div className="num text-[16px]">{gbp(est)}</div></div>
                  <div><div className="text-muted">Simulated result vs estimate</div><div className={`num text-[16px] ${signClass(pnl - est)}`}>{gbp(pnl - est)}</div></div>
                </div>
              )
            })()}
            <p className="mt-2 text-[11px] text-muted">{report.data.note}</p>
          </Card>
        </>
      )}
    </div>
  )
}
