import { useQueryClient } from '@tanstack/react-query'
import { Play, Trash2 } from 'lucide-react'
import { Fragment, useEffect, useState } from 'react'
import { del, post } from '../api'
import { EquityChart, PnlBars } from '../components/charts'
import { PageTitle } from '../components/Layout'
import {
  Badge, Button, Card, Empty, ErrorBox, Field, KV, Notice, SourceBadge, Spinner, Stat, Tabs, Tip, useConfirm,
} from '../components/ui'
import { useApi } from '../hooks'
import { dt, gbp, label, pct, signClass } from '../lib/format'

const isoLocal = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16)

interface Form {
  name: string
  strategies: string[]
  exchanges: string
  symbols: string
  start: string
  end: string
  data_source: string
  initial_capital_gbp: string
  allocation: string
  min_net_profit_pct: string
  max_trade_gbp: string
  taker_fee_override: string
  slippage_buffer_bps: string
  latency_ms: string
  latency_cost_bps: string
  order_type: string
  maker_fill_probability: string
  max_slippage_pct: string
  hedge_policy: string
  max_position_gbp: string
  max_daily_loss_gbp: string
  risk_min_net_return_pct: string
  rebalance_enabled: boolean
  rebalance_threshold_pct: string
  seed: string
}

export default function Backtests() {
  const qc = useQueryClient()
  const settings = useApi<any>('/settings')
  const runs = useApi<any[]>('/backtests', 3000)
  const avail = useApi<any[]>('/data/availability', 30000)
  const [tab, setTab] = useState('runs')
  const [selected, setSelected] = useState<number | null>(null)
  const [compare, setCompare] = useState<number[]>([])
  const [form, setForm] = useState<Form | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (form || !settings.data) return
    const s = settings.data.settings
    const end = new Date()
    const start = new Date(end.getTime() - 3600_000)
    setForm({
      name: '', strategies: ['cross_exchange'], exchanges: s.exchanges.join(','), symbols: s.symbols.join(','),
      start: isoLocal(start), end: isoLocal(end), data_source: s.market_data_mode === 'synthetic' ? 'synthetic' : 'live_recorded',
      initial_capital_gbp: s.paper_initial_capital_gbp, allocation: '', min_net_profit_pct: s.min_net_profit_pct,
      max_trade_gbp: s.max_trade_size_gbp, taker_fee_override: '', slippage_buffer_bps: '5', latency_ms: '250',
      latency_cost_bps: '2', order_type: 'taker', maker_fill_probability: '0.5', max_slippage_pct: '0.20',
      hedge_policy: 'unwind', max_position_gbp: s.max_trade_size_gbp, max_daily_loss_gbp: '25',
      risk_min_net_return_pct: s.min_net_profit_pct, rebalance_enabled: true, rebalance_threshold_pct: '80', seed: '42',
    })
  }, [settings.data, form])

  if (!form) return <Spinner />
  const set = (k: keyof Form, v: any) => setForm({ ...form, [k]: v })
  const list = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean)

  const buildConfig = () => {
    const fee: Record<string, string> = {}
    if (form.taker_fee_override) list(form.exchanges).forEach((e) => (fee[e] = String(Number(form.taker_fee_override) / 100)))
    let allocation = null
    if (form.allocation.trim()) {
      allocation = form.allocation.split(/[\n;]/).map((l) => l.trim()).filter(Boolean).map((l) => {
        const [ex, asset, pctv] = l.split(/[:,\s]+/)
        return { exchange: ex, asset, fraction: String(Number(pctv) / 100) }
      })
    }
    return {
      name: form.name, strategies: form.strategies, exchanges: list(form.exchanges), symbols: list(form.symbols),
      start: new Date(form.start).toISOString(), end: new Date(form.end).toISOString(), data_source: form.data_source,
      initial_capital_gbp: form.initial_capital_gbp, allocation,
      params: { min_net_profit_pct: form.min_net_profit_pct, max_trade_gbp: form.max_trade_gbp, fee_overrides: fee,
        slippage_buffer_bps: form.slippage_buffer_bps, latency_ms: Number(form.latency_ms), latency_cost_bps: form.latency_cost_bps },
      fill: { order_type: form.order_type, maker_fill_probability: Number(form.maker_fill_probability), max_slippage_pct: form.max_slippage_pct,
        latency_ms: Number(form.latency_ms), hedge_policy: form.hedge_policy },
      risk: { max_position_gbp: form.max_position_gbp, max_daily_loss_gbp: form.max_daily_loss_gbp, min_net_return_pct: form.risk_min_net_return_pct },
      rebalance: { enabled: form.rebalance_enabled, threshold_pct: form.rebalance_threshold_pct },
      seed: Number(form.seed),
    }
  }

  const run = async () => {
    setBusy(true); setError(null)
    try {
      const r = await post('/backtests', buildConfig())
      setSelected(r.id); setTab('runs')
      qc.invalidateQueries({ queryKey: ['/backtests'] })
    } catch (e) { setError(e) } finally { setBusy(false) }
  }

  const genSynthetic = async () => {
    setBusy(true); setError(null)
    try {
      await post('/data/synthetic', { exchanges: list(form.exchanges), symbols: list(form.symbols),
        start: new Date(form.start).toISOString(), end: new Date(form.end).toISOString(), seed: Number(form.seed) })
      set('data_source', 'synthetic')
      qc.invalidateQueries({ queryKey: ['/data/availability'] })
    } catch (e) { setError(e) } finally { setBusy(false) }
  }

  return (
    <div className="space-y-4">
      <PageTitle title="Backtesting Laboratory" subtitle="Event-driven replay of recorded order books with latency, partial fills, fees, hedging failures and inventory constraints. Results are simulated." />
      <div className="grid gap-4 xl:grid-cols-[380px_1fr]">
        <Card title="Configuration" bodyClass="space-y-3">
          <Field label="Run name"><input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="optional" /></Field>
          <Field label="Strategies" group>
            <div className="flex gap-3">
              {['cross_exchange', 'triangular'].map((s) => (
                <label key={s} className="flex items-center gap-1.5">
                  <input type="checkbox" checked={form.strategies.includes(s)}
                    onChange={(e) => set('strategies', e.target.checked ? [...form.strategies, s] : form.strategies.filter((x) => x !== s))} />
                  {label(s)}
                </label>
              ))}
            </div>
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Exchanges" hint="comma-separated"><input value={form.exchanges} onChange={(e) => set('exchanges', e.target.value)} /></Field>
            <Field label="Trading pairs"><input value={form.symbols} onChange={(e) => set('symbols', e.target.value)} /></Field>
            <Field label="Start"><input type="datetime-local" value={form.start} onChange={(e) => set('start', e.target.value)} /></Field>
            <Field label="End"><input type="datetime-local" value={form.end} onChange={(e) => set('end', e.target.value)} /></Field>
          </div>
          <Field label="Historical data source" tip="Real recorded data and synthetic test data are never mixed. Synthetic results are not evidence of real profitability.">
            <select value={form.data_source} onChange={(e) => set('data_source', e.target.value)}>
              <option value="live_recorded">Real recorded exchange data</option>
              <option value="third_party_imported">Third-party imported data</option>
              <option value="synthetic">Synthetic test data</option>
            </select>
          </Field>
          {form.data_source === 'synthetic' && <Notice>Synthetic data exercises the engine only. Results say nothing about real markets.</Notice>}
          <div className="grid grid-cols-2 gap-2">
            <Field label="Starting capital (£)"><input value={form.initial_capital_gbp} onChange={(e) => set('initial_capital_gbp', e.target.value)} /></Field>
            <Field label="Seed" tip="Seed for latency jitter and maker fill randomness — same seed + data = identical result."><input value={form.seed} onChange={(e) => set('seed', e.target.value)} /></Field>
          </div>
          <Field label="Starting allocation" tip="One line per bucket: exchange:asset:percent. Leave empty for the default (even split across exchanges, half quote currency and half base assets).">
            <textarea rows={3} value={form.allocation} onChange={(e) => set('allocation', e.target.value)} placeholder={'kraken:USD:25\nkraken:BTC:25\ncoinbase:USD:25\ncoinbase:BTC:25'} />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Min est. net profit (%)"><input value={form.min_net_profit_pct} onChange={(e) => set('min_net_profit_pct', e.target.value)} /></Field>
            <Field label="Max trade size (£)"><input value={form.max_trade_gbp} onChange={(e) => set('max_trade_gbp', e.target.value)} /></Field>
            <Field label="Taker fee override (%)" tip="Empty = exchange fee schedule recorded with the data (or 0.26% default)."><input value={form.taker_fee_override} onChange={(e) => set('taker_fee_override', e.target.value)} placeholder="exchange" /></Field>
            <Field label="Slippage buffer (bps)"><input value={form.slippage_buffer_bps} onChange={(e) => set('slippage_buffer_bps', e.target.value)} /></Field>
            <Field label="Latency (ms)"><input value={form.latency_ms} onChange={(e) => set('latency_ms', e.target.value)} /></Field>
            <Field label="Latency cost (bps/leg)"><input value={form.latency_cost_bps} onChange={(e) => set('latency_cost_bps', e.target.value)} /></Field>
            <Field label="Order type">
              <select value={form.order_type} onChange={(e) => set('order_type', e.target.value)}><option value="taker">Taker (IOC limit)</option><option value="maker">Maker (resting)</option></select>
            </Field>
            {form.order_type === 'maker'
              ? <Field label="Maker fill probability"><input value={form.maker_fill_probability} onChange={(e) => set('maker_fill_probability', e.target.value)} /></Field>
              : <Field label="Limit tolerance (%)" tip="How far beyond the estimated VWAP the IOC limit price may be."><input value={form.max_slippage_pct} onChange={(e) => set('max_slippage_pct', e.target.value)} /></Field>}
            <Field label="Leg mismatch policy">
              <select value={form.hedge_policy} onChange={(e) => set('hedge_policy', e.target.value)}><option value="unwind">Unwind immediately</option><option value="hold">Hold inventory</option></select>
            </Field>
            <Field label="Risk: max position (£)"><input value={form.max_position_gbp} onChange={(e) => set('max_position_gbp', e.target.value)} /></Field>
            <Field label="Risk: max daily loss (£)"><input value={form.max_daily_loss_gbp} onChange={(e) => set('max_daily_loss_gbp', e.target.value)} /></Field>
            <Field label="Risk: min net return (%)"><input value={form.risk_min_net_return_pct} onChange={(e) => set('risk_min_net_return_pct', e.target.value)} /></Field>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5"><input type="checkbox" checked={form.rebalance_enabled} onChange={(e) => set('rebalance_enabled', e.target.checked)} /> Rebalance inventory</label>
            <input className="w-20" value={form.rebalance_threshold_pct} onChange={(e) => set('rebalance_threshold_pct', e.target.value)} title="Rebalance when one exchange holds more than this % of an asset" /><span className="text-[11px] text-muted">% threshold</span>
          </div>
          <ErrorBox error={error} />
          <div className="flex gap-2">
            <Button variant="primary" onClick={run} disabled={busy || !form.strategies.length}><Play size={14} /> Run backtest</Button>
            <Button onClick={genSynthetic} disabled={busy} title="Generate a labelled synthetic dataset for this window">Generate synthetic data</Button>
          </div>
        </Card>

        <div className="min-w-0 space-y-4">
          <Tabs value={tab} onChange={setTab} tabs={[{ id: 'runs', label: 'Runs' }, { id: 'compare', label: `Compare (${compare.length})` }, { id: 'walkforward', label: 'Walk-forward' }, { id: 'data', label: 'Data availability' }]} />
          {tab === 'runs' && (
            <>
              <RunsTable runs={runs.data ?? []} selected={selected} onSelect={setSelected} compare={compare} setCompare={setCompare} />
              {selected && <RunDetail id={selected} />}
            </>
          )}
          {tab === 'compare' && <Compare ids={compare} />}
          {tab === 'walkforward' && <WalkForward buildConfig={buildConfig} />}
          {tab === 'data' && (
            <Card title="Recorded historical data" bodyClass="p-0">
              {(avail.data ?? []).length === 0 ? <Empty>No data recorded yet. Run with market data mode "live" and recording enabled, or generate a synthetic dataset.</Empty> : (
                <table className="data">
                  <thead><tr><th>Source</th><th>Exchange</th><th>Symbol</th><th className="r">Snapshots</th><th>First</th><th>Last</th></tr></thead>
                  <tbody>{avail.data!.map((a, i) => (
                    <tr key={i}><td><SourceBadge source={a.source} /></td><td>{a.exchange}</td><td>{a.symbol}</td><td className="r num">{a.snapshots}</td><td>{dt(a.first)}</td><td>{dt(a.last)}</td></tr>
                  ))}</tbody>
                </table>
              )}
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

function RunsTable({ runs, selected, onSelect, compare, setCompare }: { runs: any[]; selected: number | null; onSelect: (id: number) => void; compare: number[]; setCompare: (c: number[]) => void }) {
  const qc = useQueryClient()
  const { ask, dialog } = useConfirm()
  return (
    <Card title="Backtest runs" bodyClass="p-0 max-h-72 overflow-auto">
      {runs.length === 0 ? <Empty>No backtests yet.</Empty> : (
        <table className="data">
          <thead><tr><th></th><th>#</th><th>Name</th><th>Status</th><th>Data</th><th>Period</th><th className="r">P&amp;L</th><th className="r">Return</th><th className="r">Trades</th><th></th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id} className={`cursor-pointer ${selected === r.id ? 'bg-panel2' : ''}`} onClick={() => onSelect(r.id)}>
                <td onClick={(e) => e.stopPropagation()}><input type="checkbox" checked={compare.includes(r.id)} onChange={(e) => setCompare(e.target.checked ? [...compare, r.id] : compare.filter((x) => x !== r.id))} /></td>
                <td className="num">{r.id}</td>
                <td className="max-w-[220px] truncate" title={r.name}>{r.name}{r.walkforward_segment && <Badge tone="muted">WF {r.walkforward_segment}</Badge>}</td>
                <td><Badge tone={r.status === 'completed' ? 'pos' : r.status === 'failed' ? 'neg' : 'accent'}>{r.status}{r.status === 'running' ? ` ${Math.round((r.progress ?? 0) * 100)}%` : ''}</Badge></td>
                <td><SourceBadge source={r.data_source} /></td>
                <td className="text-[11px] text-muted">{dt(r.start)} → {dt(r.end)}</td>
                <td className={`r num ${signClass(r.headline?.total_pnl_gbp)}`}>{gbp(r.headline?.total_pnl_gbp)}</td>
                <td className={`r num ${signClass(r.headline?.net_return_pct)}`}>{pct(r.headline?.net_return_pct)}</td>
                <td className="r num">{r.headline?.trades_attempted ?? '—'}</td>
                <td onClick={(e) => e.stopPropagation()}>
                  <button className="text-muted hover:text-neg" title="Delete run" onClick={() => ask({ title: `Delete backtest #${r.id}?`, message: 'The run and its trades will be removed permanently.', danger: true, label: 'Delete', action: async () => { await del(`/backtests/${r.id}`); qc.invalidateQueries({ queryKey: ['/backtests'] }) } })}><Trash2 size={13} /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {dialog}
    </Card>
  )
}

function RunDetail({ id }: { id: number }) {
  const run = useApi<any>(`/backtests/${id}`, 2000)
  const trades = useApi<any[]>(run.data?.status === 'completed' ? `/backtests/${id}/trades?limit=500` : null)
  const r = run.data
  if (!r) return <Spinner />
  if (r.status === 'queued' || r.status === 'running') return <Card><Spinner label={`Running… ${Math.round((r.progress ?? 0) * 100)}%`} /></Card>
  if (r.status === 'failed') return <Card title={`Run #${id} failed`}><ErrorBox error={r.error} /></Card>
  const m = r.metrics
  const curve = (r.equity_curve ?? []).map((p: any) => ({ ts: p.ts, value: Number(p.value_gbp) }))
  return (
    <div className="space-y-4">
      {(m.warnings ?? []).length > 0 && <Notice>{m.warnings.map((w: string, i: number) => <div key={i}>• {w}</div>)}</Notice>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 2xl:grid-cols-8">
        <Stat label="Initial" value={gbp(m.initial_value_gbp)} />
        <Stat label="Final" value={gbp(m.final_value_gbp)} />
        <Stat label="Total P&L" value={gbp(m.total_pnl_gbp)} tone={signClass(m.total_pnl_gbp)} sub="simulated" />
        <Stat label="Net return" value={pct(m.net_return_pct)} tone={signClass(m.net_return_pct)} />
        <Stat label="Max drawdown" value={pct(m.max_drawdown_pct, 2)} sub={gbp(m.max_drawdown_gbp)} tip="max_drawdown" />
        <Stat label="Fees paid" value={gbp(m.fees_paid_gbp)} />
        <Stat label="Fill ratio" value={m.fill_ratio == null ? '—' : `${(m.fill_ratio * 100).toFixed(1)}%`} tip="fill_ratio" />
        <Stat label="Sharpe" value={m.sharpe_ratio == null ? 'n/a' : m.sharpe_ratio.toFixed(2)} sub={m.sharpe_note} tip="sharpe" />
      </div>
      <Card title="Equity curve (simulated)"><EquityChart series={[{ name: `#${id}`, points: curve }]} /></Card>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Activity">
          <KV rows={[
            ['Opportunities detected', m.opportunities_detected], ['Eligible (qualified)', m.opportunities_eligible],
            ['Trades attempted', m.trades_attempted], ['Successful cycles', m.successful_cycles],
            ['Partial (hedged/held)', m.partial_cycles], ['Failed', m.failed_cycles],
            ['Trading volume', gbp(m.trading_volume_gbp)], ['Avg net margin', pct(m.avg_net_margin_pct)],
            ['Rebalances', `${m.rebalances} (${gbp(m.rebalancing_costs_gbp)})`], ['Events replayed', m.events_processed],
          ]} />
        </Card>
        <Card title="P&L decomposition">
          <KV rows={[
            [<>Trade P&amp;L <Tip k="realised_trade_pnl" /></>, <span className={signClass(m.realised_trade_pnl_gbp)}>{gbp(m.realised_trade_pnl_gbp)}</span>],
            [<>Inventory / market P&amp;L <Tip k="market_pnl" /></>, <span className={signClass(m.market_and_inventory_pnl_gbp)}>{gbp(m.market_and_inventory_pnl_gbp)}</span>],
            ['Rebalancing costs', gbp(m.rebalancing_costs_gbp)],
            ['Fees (inside trade P&L)', gbp(m.fees_paid_gbp)],
            ['Slippage vs estimate', gbp(m.slippage_losses_gbp)],
            [<>Failed hedge losses <Tip k="failed_hedge_losses" /></>, gbp(m.failed_hedge_losses_gbp)],
            ['Max inventory exposure', gbp(m.max_inventory_exposure_gbp)],
            ['Final inventory exposure', gbp(m.final_inventory_exposure_gbp)],
          ]} />
        </Card>
        <Card title="Rejections">
          <div className="space-y-1 text-[12px]">
            <div className="text-muted">Strategy filters</div>
            {Object.entries(m.rejection_reasons ?? {}).map(([k, v]) => <div key={k} className="flex justify-between"><span>{label(k)}</span><span className="num">{String(v)}</span></div>)}
            <div className="pt-2 text-muted">Risk engine</div>
            {Object.entries(m.risk_rejections ?? {}).map(([k, v]) => <div key={k} className="flex justify-between"><span>{label(k)}</span><span className="num">{String(v)}</span></div>)}
            {!Object.keys(m.risk_rejections ?? {}).length && <div className="text-muted">none</div>}
          </div>
        </Card>
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="P&L by pair / route"><PnlBars data={Object.entries(m.pnl_by_pair ?? {}).map(([k, v]: any) => ({ name: k.replace(/^\w+: /, ''), value: Number(v.pnl_gbp) }))} /></Card>
        <Card title="P&L by exchange (fill value at marks)"><PnlBars data={Object.entries(m.pnl_by_exchange ?? {}).map(([k, v]) => ({ name: k, value: Number(v) }))} /></Card>
        <Card title="P&L by hour (UTC)"><PnlBars data={Object.entries(m.pnl_by_hour_utc ?? {}).map(([k, v]: any) => ({ name: `${k}h`, value: Number(v.pnl_gbp) }))} /></Card>
      </div>
      <Card title="Reproducibility">
        <KV rows={[['Dataset id', r.dataset_id], ['Data source', r.data_source], ['Code revision', r.code_revision], ['Seed', r.seed], ['Result hash', <span className="text-[11px]">{r.result_hash}</span>], ['Snapshots', r.dataset_summary?.snapshots]]} />
      </Card>
      <Card title={`Simulated trades (${trades.data?.length ?? 0})`} bodyClass="p-0 max-h-96 overflow-auto" actions={<a className="text-[12px] text-accent" href={`/api/reports/csv?source=backtest&run_id=${id}`}>Export CSV</a>}>
        {!trades.data?.length ? <Empty>No trades executed.</Empty> : <TradesTable trades={trades.data} />}
      </Card>
    </div>
  )
}

export function TradesTable({ trades }: { trades: any[] }) {
  const [open, setOpen] = useState<number | null>(null)
  return (
    <table className="data">
      <thead><tr><th>Time</th><th>Kind</th><th>Route</th><th>Status</th><th className="r">Notional</th><th className="r">Est. net</th><th className="r">Simulated P&amp;L</th><th className="r">Fees</th><th className="r">Hedge loss</th></tr></thead>
      <tbody>
        {trades.map((t, i) => (
          <Fragment key={t.id ?? i}>
            <tr className="cursor-pointer" onClick={() => setOpen(open === i ? null : i)}>
              <td className="text-[11px]">{dt(t.ts)}</td><td className="text-muted">{t.kind === 'triangular' ? 'tri' : 'cross'}</td>
              <td>{t.kind === 'triangular' ? `${t.exchanges[0]}: ${t.route.join('→')}` : `${t.route[1]} ${t.exchanges.join(' → ')}`}</td>
              <td><Badge tone={t.status === 'success' ? 'pos' : t.status === 'failed' ? 'neg' : 'warn'}>{label(t.status)}</Badge></td>
              <td className="r num">{gbp(t.notional_gbp)}</td><td className="r num">{gbp(t.expected_net_gbp)}</td>
              <td className={`r num ${signClass(t.pnl_gbp)}`}>{gbp(t.pnl_gbp)}</td><td className="r num">{gbp(t.fees_gbp)}</td><td className="r num">{gbp(t.hedge_loss_gbp)}</td>
            </tr>
            {open === i && (
              <tr><td colSpan={9} className="bg-panel2">
                <div className="space-y-1 text-[11px]">
                  {(t.detail?.notes ?? []).map((n: string, j: number) => <div key={j} className="text-warn">• {n}</div>)}
                  {(t.detail?.fills ?? []).map((f: any, j: number) => (
                    <div key={j} className="num">{f.purpose.padEnd(6)} {f.exchange} {f.symbol} {f.side} req {f.requested_base} filled {f.filled_base} @ {f.vwap ?? '—'} fee {f.fee} {f.fee_currency} [{f.status}] {f.reason}</div>
                  ))}
                  <div className="text-muted">latency {Math.round(t.detail?.latency_ms ?? 0)} ms</div>
                </div>
              </td></tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
  )
}

function Compare({ ids }: { ids: number[] }) {
  const data = useApi<any[]>(ids.length ? `/backtests/compare?ids=${ids.join(',')}` : null)
  if (!ids.length) return <Card><Empty>Tick runs in the Runs tab to compare them side by side.</Empty></Card>
  if (!data.data) return <Spinner />
  const rows: [string, (m: any, r: any) => any][] = [
    ['Strategies', (_m, r) => (r.strategies ?? []).join('+')], ['Data', (_m, r) => r.data_source],
    ['Exchanges', (_m, r) => (r.exchanges ?? []).join(', ')], ['Pairs', (_m, r) => (r.symbols ?? []).join(', ')],
    ['Min profit %', (_m, r) => r.config?.params?.min_net_profit_pct], ['Total P&L', (m) => gbp(m.total_pnl_gbp)],
    ['Net return', (m) => pct(m.net_return_pct)], ['Max drawdown', (m) => pct(m.max_drawdown_pct, 2)],
    ['Trades', (m) => m.trades_attempted], ['Successful', (m) => m.successful_cycles], ['Fill ratio', (m) => m.fill_ratio?.toFixed(3) ?? '—'],
    ['Fees', (m) => gbp(m.fees_paid_gbp)], ['Hedge losses', (m) => gbp(m.failed_hedge_losses_gbp)],
    ['Avg net margin', (m) => pct(m.avg_net_margin_pct)], ['Detected / eligible', (m) => `${m.opportunities_detected} / ${m.opportunities_eligible}`],
  ]
  const done = data.data.filter((r) => r.status === 'completed')
  return (
    <div className="space-y-4">
      <Card title="Equity curves (elapsed time)">
        <EquityChart series={done.map((r) => ({ name: `#${r.id}`, points: (r.equity_curve ?? []).map((p: any) => ({ ts: p.ts, value: Number(p.value_gbp) })) }))} />
      </Card>
      <Card bodyClass="p-0 overflow-auto">
        <table className="data">
          <thead><tr><th>Metric</th>{done.map((r) => <th key={r.id} className="r">#{r.id} {r.name}</th>)}</tr></thead>
          <tbody>{rows.map(([k, f]) => <tr key={k}><td className="text-muted">{k}</td>{done.map((r) => <td key={r.id} className="r num">{f(r.metrics, r)}</td>)}</tr>)}</tbody>
        </table>
      </Card>
    </div>
  )
}

function WalkForward({ buildConfig }: { buildConfig: () => any }) {
  const qc = useQueryClient()
  const studies = useApi<any[]>('/walkforward', 3000)
  const [grid, setGrid] = useState('0.1,0.2,0.3,0.5')
  const [objective, setObjective] = useState('total_pnl_gbp')
  const [error, setError] = useState<unknown>(null)
  const { ask, dialog } = useConfirm()
  const create = async () => {
    setError(null)
    try {
      await post('/walkforward', { base: buildConfig(), param_grid: { min_net_profit_pct: grid.split(',').map((x) => x.trim()) }, objective })
      qc.invalidateQueries({ queryKey: ['/walkforward'] })
    } catch (e) { setError(e) }
  }
  return (
    <div className="space-y-4">
      <Card title="New walk-forward study" bodyClass="space-y-3">
        <p className="text-[12px] text-muted">Uses the configuration on the left. The period is split 60% training / 20% validation / 20% out-of-sample test. Parameters are chosen on training, confirmed on validation and then <b>locked</b>; the test segment stays sealed until you run it, once.</p>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Min net profit % grid"><input value={grid} onChange={(e) => setGrid(e.target.value)} /></Field>
          <Field label="Objective"><select value={objective} onChange={(e) => setObjective(e.target.value)}><option value="total_pnl_gbp">Total P&amp;L</option><option value="net_return_pct">Net return</option><option value="sharpe_ratio">Sharpe</option><option value="successful_cycles">Successful cycles</option></select></Field>
          <Button variant="primary" onClick={create}>Start optimisation</Button>
        </div>
        <ErrorBox error={error} />
      </Card>
      {(studies.data ?? []).map((s) => (
        <Card key={s.id} title={`Study #${s.id}`} actions={<Badge tone={s.status === 'tested' ? 'pos' : s.status === 'failed' ? 'neg' : s.status === 'locked' ? 'sim' : 'accent'}>{s.status}</Badge>}>
          <div className="grid gap-3 text-[12px] md:grid-cols-3">
            {Object.entries(s.windows).map(([k, w]: any) => <div key={k}><span className="text-muted">{k}: </span>{dt(w[0])} → {dt(w[1])}</div>)}
          </div>
          {s.error && <ErrorBox error={s.error} />}
          <div className="mt-3 grid gap-4 md:grid-cols-2">
            <div><div className="mb-1 text-[11px] uppercase text-muted">Training</div>
              {s.train_results.map((r: any) => <div key={r.run_id} className="flex justify-between text-[12px]"><span>{JSON.stringify(r.params)}</span><span className="num">{Number(r.objective).toFixed(4)}</span></div>)}</div>
            <div><div className="mb-1 text-[11px] uppercase text-muted">Validation</div>
              {s.validation_results.map((r: any) => <div key={r.run_id} className="flex justify-between text-[12px]"><span>{JSON.stringify(r.params)}</span><span className="num">{Number(r.objective).toFixed(4)}</span></div>)}</div>
          </div>
          {s.selected_params && <div className="mt-2 text-[12px]">Locked parameters: <b>{JSON.stringify(s.selected_params)}</b></div>}
          {s.status === 'locked' && (
            <div className="mt-3"><Button variant="primary" onClick={() => ask({ title: 'Run out-of-sample test?', message: 'This evaluates the locked parameters on the sealed test segment. It can only be run once per study.', label: 'Run test', action: async () => { await post(`/walkforward/${s.id}/test`); qc.invalidateQueries({ queryKey: ['/walkforward'] }) } })}>Run sealed test</Button></div>
          )}
          {s.test_result && typeof s.test_result === 'object' && (
            <div className="mt-3 rounded border border-line p-2 text-[12px]">Out-of-sample result (run #{s.test_result.run_id}): objective <b className="num">{Number(s.test_result.objective).toFixed(4)}</b>, P&amp;L <b className={signClass(s.test_result.metrics?.total_pnl_gbp)}>{gbp(s.test_result.metrics?.total_pnl_gbp)}</b></div>
          )}
        </Card>
      ))}
      {dialog}
    </div>
  )
}
