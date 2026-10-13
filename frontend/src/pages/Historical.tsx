import { useQueryClient } from '@tanstack/react-query'
import { Download, Play, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { del, post } from '../api'
import { DateSeriesChart, PnlBars } from '../components/charts'
import { SERIES } from '../lib/colors'
import { PageTitle } from '../components/Layout'
import { Badge, Button, Card, Empty, ErrorBox, Field, KV, Notice, Spinner, Stat, Tabs, useConfirm } from '../components/ui'
import { useApi } from '../hooks'
import { dt, gbp, label, pct, signClass } from '../lib/format'

interface Form {
  name: string
  initial: string
  monthly: string
  start: string
  end: string
  exchanges: string
  symbols: string
  strategies: string[]
  fee_schedule: string
  preset: string
  min_net_profit_pct: string
  max_trade_gbp: string
  max_position_gbp: string
  conversion_cost_bps: string
  crypto_inventory_pct: string
  run_parameter_study: boolean
}

const STRATS: [string, string][] = [['cross_exchange', 'A: cross-exchange'], ['triangular', 'B: triangular'], ['combined', 'C: combined']]

export default function Historical() {
  const qc = useQueryClient()
  const defaults = useApi<any>('/historical/defaults')
  const sims = useApi<any[]>('/historical/simulations', 3000)
  const [form, setForm] = useState<Form | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (form || !defaults.data) return
    const c = defaults.data.config
    setForm({
      name: c.name, initial: c.plan.initial_amount, monthly: c.plan.recurring_amount, start: c.start.slice(0, 10),
      end: c.end.slice(0, 10), exchanges: c.exchanges.join(','), symbols: c.symbols.join(','), strategies: c.strategies,
      fee_schedule: c.fee_schedule, preset: c.preset, min_net_profit_pct: c.params.min_net_profit_pct,
      max_trade_gbp: c.params.max_trade_gbp, max_position_gbp: c.risk.max_position_gbp,
      conversion_cost_bps: c.plan.conversion_cost_bps, crypto_inventory_pct: c.crypto_inventory_pct,
      run_parameter_study: c.run_parameter_study,
    })
  }, [defaults.data, form])

  useEffect(() => {
    if (selected === null && sims.data?.length) setSelected(sims.data[0].id)
  }, [sims.data, selected])

  if (!form) return <Spinner />
  const set = (k: keyof Form, v: any) => setForm({ ...form, [k]: v })
  const list = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean)

  const run = async () => {
    setBusy(true); setError(null)
    try {
      const r = await post('/historical/simulations', {
        name: form.name, start: `${form.start}T00:00:00Z`, end: `${form.end}T23:59:59Z`,
        plan: { initial_amount: form.initial, recurring_amount: form.monthly, conversion_cost_bps: form.conversion_cost_bps },
        exchanges: list(form.exchanges), symbols: list(form.symbols), strategies: form.strategies,
        fee_schedule: form.fee_schedule, preset: form.preset, run_parameter_study: form.run_parameter_study,
        crypto_inventory_pct: form.crypto_inventory_pct,
        params: { min_net_profit_pct: form.min_net_profit_pct, max_trade_gbp: form.max_trade_gbp },
        risk: { max_position_gbp: form.max_position_gbp, min_net_return_pct: form.min_net_profit_pct },
      })
      setSelected(r.id)
      qc.invalidateQueries({ queryKey: ['/historical/simulations'] })
    } catch (e) { setError(e) } finally { setBusy(false) }
  }

  const c = defaults.data?.contributions
  return (
    <div className="space-y-4">
      <PageTitle title="Historical Investment Simulator"
        subtitle="What a starting sum plus monthly contributions would have become, replaying genuine exchange order books through the Arbies strategies. Simulated, and only as complete as the historical data." />
      <div className="grid gap-4 xl:grid-cols-[360px_1fr]">
        <div className="space-y-4">
          <Card title="Scenario" bodyClass="space-y-3">
            <Field label="Name"><input value={form.name} onChange={(e) => set('name', e.target.value)} /></Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Initial investment (£)"><input value={form.initial} onChange={(e) => set('initial', e.target.value)} /></Field>
              <Field label="Monthly contribution (£)" tip="Deposited at 00:00 UTC on the 1st of each month after the start date."><input value={form.monthly} onChange={(e) => set('monthly', e.target.value)} /></Field>
              <Field label="Start date"><input type="date" value={form.start} onChange={(e) => set('start', e.target.value)} /></Field>
              <Field label="End date"><input type="date" value={form.end} onChange={(e) => set('end', e.target.value)} /></Field>
            </div>
            {c && form.initial === defaults.data.config.plan.initial_amount && form.monthly === defaults.data.config.plan.recurring_amount && (
              <div className="text-[11px] text-muted">Default scenario: £{c.initial} + {c.recurring_count} × £{form.monthly} = £{c.total} personal contributions.</div>
            )}
            <Field label="Strategies" group>
              <div className="flex flex-wrap gap-3">
                {STRATS.map(([id, lbl]) => (
                  <label key={id} className="flex items-center gap-1.5">
                    <input type="checkbox" checked={form.strategies.includes(id)}
                      onChange={(e) => set('strategies', e.target.checked ? [...form.strategies, id] : form.strategies.filter((x) => x !== id))} />
                    {lbl}
                  </label>
                ))}
              </div>
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Exchanges"><input value={form.exchanges} onChange={(e) => set('exchanges', e.target.value)} /></Field>
              <Field label="Trading pairs"><input value={form.symbols} onChange={(e) => set('symbols', e.target.value)} /></Field>
              <Field label="Fee assumptions" tip="Dated retail fee schedules researched for 2025–2026. 'base' is conservative where sources conflict; 'low' and 'high' bracket it.">
                <select value={form.fee_schedule} onChange={(e) => set('fee_schedule', e.target.value)}>
                  <option value="base">Base (conservative retail)</option><option value="low">Low</option><option value="high">High (+25%)</option>
                </select>
              </Field>
              <Field label="Parameter preset">
                <select value={form.preset} onChange={(e) => set('preset', e.target.value)}>
                  <option value="conservative">Conservative</option><option value="moderate">Moderate</option><option value="aggressive">Aggressive</option>
                </select>
              </Field>
              <Field label="Min net profit (%)"><input value={form.min_net_profit_pct} onChange={(e) => set('min_net_profit_pct', e.target.value)} /></Field>
              <Field label="Max trade size (£)"><input value={form.max_trade_gbp} onChange={(e) => set('max_trade_gbp', e.target.value)} /></Field>
              <Field label="Risk: max position (£)"><input value={form.max_position_gbp} onChange={(e) => set('max_position_gbp', e.target.value)} /></Field>
              <Field label="Crypto inventory (%)" tip="Share of each deposit held as BTC/ETH so cross-exchange trades can sell without transfers. This inventory carries market risk."><input value={form.crypto_inventory_pct} onChange={(e) => set('crypto_inventory_pct', e.target.value)} /></Field>
              <Field label="Deposit conversion cost (bps)" tip="Fee + spread paid when deposits are converted into USD/BTC/ETH."><input value={form.conversion_cost_bps} onChange={(e) => set('conversion_cost_bps', e.target.value)} /></Field>
            </div>
            <label className="flex items-center gap-1.5"><input type="checkbox" checked={form.run_parameter_study} onChange={(e) => set('run_parameter_study', e.target.checked)} /> Conservative / aggressive and fee sensitivity study</label>
            <ErrorBox error={error} />
            <Button variant="primary" onClick={run} disabled={busy || !form.strategies.length}><Play size={14} /> Run simulation</Button>
          </Card>
          <CoverageCard start={form.start} />
          <SimList sims={sims.data ?? []} selected={selected} onSelect={setSelected} />
        </div>
        <div className="min-w-0">{selected ? <SimDetail id={selected} /> : <Card><Empty>No simulations yet. Configure a scenario and run it.</Empty></Card>}</div>
      </div>
    </div>
  )
}

function CoverageCard({ start }: { start: string }) {
  const cov = useApi<any>(`/historical/coverage?start=${start}T00:00:00`, 60000)
  const refs = useApi<any[]>('/historical/reference', 60000)
  const ds = useApi<any[]>('/historical/datasets', 60000)
  const d = cov.data
  const tone = !d ? 'muted' : d.continuous ? 'pos' : d.overlap_minutes ? 'warn' : 'neg'
  return (
    <Card title="Historical data coverage" tip="Genuine order books only (live-recorded and imported). Trading can only be simulated where they exist.">
      {!d ? <Spinner /> : (
        <div className="space-y-2 text-[12px]">
          <div className="flex items-center gap-2"><Badge tone={tone}>{d.continuous ? 'continuous' : 'incomplete'}</Badge>
            <span className="num">{d.overlap_pct}%</span><span className="text-muted">of minutes have ≥ 2 exchanges' books</span></div>
          <div className="flex h-3 gap-px" title="Order-book coverage by month">
            {d.months.map((m: any) => (
              <div key={m.month} className="flex-1 rounded-sm" title={`${m.month}: ${m.overlap_pct}%`}
                   style={{ background: m.overlap_minutes ? 'var(--warn)' : 'var(--panel-2)', opacity: m.overlap_minutes ? Math.max(0.35, m.overlap_pct / 100) : 1 }} />
            ))}
          </div>
          <div className="text-muted">{(ds.data ?? []).length} imported dataset(s) · {(refs.data ?? []).filter((r) => r.interval === '1d').length} daily reference series · {(refs.data ?? []).filter((r) => r.interval === '1m').length} minute series</div>
        </div>
      )}
    </Card>
  )
}

function SimList({ sims, selected, onSelect }: { sims: any[]; selected: number | null; onSelect: (id: number) => void }) {
  const qc = useQueryClient()
  const { ask, dialog } = useConfirm()
  return (
    <Card title="Simulations" bodyClass="p-0 max-h-72 overflow-auto">
      {sims.length === 0 ? <Empty>None yet.</Empty> : (
        <table className="data">
          <thead><tr><th>#</th><th>Status</th><th className="r">Ending value</th><th></th></tr></thead>
          <tbody>{sims.map((s) => (
            <tr key={s.id} className={`cursor-pointer ${selected === s.id ? 'bg-panel2' : ''}`} onClick={() => onSelect(s.id)}>
              <td className="num">{s.id}</td>
              <td><Badge tone={s.status === 'completed' ? (s.headline?.answerable ? 'pos' : 'warn') : s.status === 'failed' ? 'neg' : 'accent'}>
                {s.status === 'completed' ? (s.headline?.answerable ? 'complete' : 'partial data') : s.status}{s.status === 'running' ? ` ${Math.round((s.progress ?? 0) * 100)}%` : ''}</Badge></td>
              <td className="r num">{gbp(s.headline?.ending_value_gbp)}</td>
              <td onClick={(e) => e.stopPropagation()}>
                <button className="text-muted hover:text-neg" title="Delete" onClick={() => ask({ title: `Delete simulation #${s.id}?`, message: 'Results and ledger are removed permanently.', danger: true, label: 'Delete', action: async () => { await del(`/historical/simulations/${s.id}`); qc.invalidateQueries({ queryKey: ['/historical/simulations'] }) } })}><Trash2 size={13} /></button>
              </td>
            </tr>))}
          </tbody>
        </table>
      )}
      {dialog}
    </Card>
  )
}

function SimDetail({ id }: { id: number }) {
  const sim = useApi<any>(`/historical/simulations/${id}`, 2000)
  const [tab, setTab] = useState('overview')
  const s = sim.data
  if (!s) return <Spinner />
  if (s.status === 'queued' || s.status === 'running') return <Card><Spinner label={`Simulating… ${Math.round((s.progress ?? 0) * 100)}%`} /></Card>
  if (s.status === 'failed' || s.status === 'partial') return (
    <Card title={s.status === 'partial' ? `Simulation #${id} is partially complete` : `Simulation #${id} failed`}>
      <div className="space-y-3">
        <ErrorBox error={s.error} />
        <div className="text-[12px] text-muted">Finished variants and the order-book cache are kept, so resuming only runs what is left.</div>
        <Button onClick={async () => { await post(`/historical/simulations/${id}/resume`); sim.refetch() }}><Play size={14} /> Resume</Button>
      </div>
    </Card>)
  const r = s.result
  const h = r.headline
  const v = r.variants[h.primary_variant]
  return (
    <div className="space-y-4">
      <Notice tone={h.answerable ? 'accent' : 'warn'}>
        <div className="font-semibold">{h.answerable ? 'Complete historical simulation' : 'Partial result — not an answer to the full-period question'}</div>
        <div>{h.status}. {h.ending_value_meaning}</div>
      </Notice>
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Total personal contributions" value={gbp(h.total_personal_contributions_gbp)} sub={`£${r.contributions.initial} initial + ${h.contribution_count} monthly`} />
        <Stat label="Trading profit / loss" value={<span className={signClass(h.trading_pnl_gbp)}>{gbp(h.trading_pnl_gbp)}</span>} sub={h.trading_pnl_scope} tone="sim" />
        <Stat label="Ending portfolio value" value={gbp(h.ending_value_gbp)} sub={<>{h.answerable ? 'simulated' : 'contributions + inventory revaluation + observed trading'}{h.valuation_basis ? <div className="text-[10px]">{h.valuation_basis}</div> : null}</>} tone={h.answerable ? undefined : 'warn'} />
      </div>
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="Market / inventory P&L" value={<span className={signClass(h.market_inventory_pnl_gbp)}>{gbp(h.market_inventory_pnl_gbp)}</span>} sub="price moves on held BTC/ETH/USD" />
        <Stat label="Conversion costs" value={gbp(h.conversion_costs_gbp)} sub="converting deposits" />
        <Stat label="Time-weighted return" value={pct(v.summary.time_weighted_return_pct, 2)} sub="excludes deposits" />
        <Stat label="Money-weighted (XIRR, annual)" value={v.summary.money_weighted_return_annual_pct === null ? '—' : `${v.summary.money_weighted_return_annual_pct.toFixed(2)}%`} sub={`coverage ${v.summary.trading_coverage_pct}%`} />
      </div>
      <Tabs value={tab} onChange={setTab} tabs={[{ id: 'overview', label: 'Charts' }, { id: 'monthly', label: 'Monthly' }, { id: 'compare', label: 'Strategies & benchmarks' },
        { id: 'study', label: 'Parameters & fees' }, { id: 'quality', label: 'Data quality' }, { id: 'ledger', label: 'Ledger' }]} />
      {tab === 'overview' && <Charts r={r} />}
      {tab === 'monthly' && <Monthly id={id} r={r} />}
      {tab === 'compare' && <Compare r={r} />}
      {tab === 'study' && <Study r={r} />}
      {tab === 'quality' && <Quality r={r} s={s} />}
      {tab === 'ledger' && <Ledger id={id} r={r} />}
    </div>
  )
}

function Charts({ r }: { r: any }) {
  const h = r.headline
  const v = r.variants[h.primary_variant]
  const series = [
    { name: `Arbies ${v.label}`, color: SERIES[0], points: v.equity.map((p: any) => ({ ts: p.ts, value: Number(p.value_gbp) })) },
    { name: 'Personal contributions (cash)', color: SERIES[1], dashed: true, points: v.equity.map((p: any) => ({ ts: p.ts, value: Number(p.contributions_gbp) })) },
  ]
  if (r.benchmarks.btc?.equity_weekly) series.push({ name: 'Bitcoin buy & hold', color: SERIES[2], points: r.benchmarks.btc.equity_weekly.map((p: any) => ({ ts: p.ts, value: Number(p.value_gbp) })) } as any)
  const gains = [
    { name: `Arbies ${v.label}`, color: SERIES[0], points: v.equity.map((p: any) => ({ ts: p.ts, value: Number(p.net_gain_gbp) })) },
    ...(r.benchmarks.btc?.equity_weekly ? [{ name: 'Bitcoin buy & hold', color: SERIES[2], points: r.benchmarks.btc.equity_weekly.map((p: any) => ({ ts: p.ts, value: Number(p.value_gbp) - Number(p.contributions_gbp) })) }] : []),
  ]
  const monthlyPnl = v.monthly.filter((m: any) => m.trading_pnl_gbp !== null).map((m: any) => ({ name: m.month, value: Number(m.trading_pnl_gbp) }))
  return (
    <div className="space-y-4">
      <Card title="Portfolio value vs contributions vs Bitcoin" tip="Arbies values are simulated. Where no order-book data exists, inventory is valued at genuine daily exchange closes and no trading happens.">
        <DateSeriesChart series={series} />
      </Card>
      <Card title="Net gain or loss (value − contributions)"><DateSeriesChart series={gains} /></Card>
      <Card title="Monthly trading P&L (observed months only)">
        {monthlyPnl.length ? <PnlBars data={monthlyPnl} /> : <Empty>No month contains order-book data, so no trading P&L can be shown.</Empty>}
      </Card>
    </div>
  )
}

function Monthly({ id, r }: { id: number; r: any }) {
  const [variant, setVariant] = useState(r.headline.primary_variant)
  const v = r.variants[variant]
  return (
    <Card title="Monthly breakdown" bodyClass="p-0 overflow-auto"
      actions={<div className="flex items-center gap-2">
        <select value={variant} onChange={(e) => setVariant(e.target.value)}>{Object.keys(r.variants).map((k) => <option key={k} value={k}>{k}</option>)}</select>
        <a className="inline-flex items-center gap-1.5 rounded-md border border-line bg-panel2 px-2 py-1 text-[12px] font-medium hover:border-accent/60" href={`/api/historical/simulations/${id}/csv?kind=monthly&variant=${encodeURIComponent(variant)}`}><Download size={13} /> CSV</a>
      </div>}>
      <table className="data">
        <thead><tr><th>Month</th><th className="r">Personal deposits</th><th className="r">Trading P&amp;L</th><th className="r">Fees</th><th className="r">Market P&amp;L</th><th className="r">Conversion</th><th className="r">Ending portfolio</th><th className="r">Trades</th><th className="r">Observed</th></tr></thead>
        <tbody>{v.monthly.map((m: any) => (
          <tr key={m.month}>
            <td>{new Date(`${m.month}-01T00:00:00Z`).toLocaleDateString('en-GB', { month: 'long', year: 'numeric' })}</td>
            <td className="r num">{gbp(m.deposits_gbp)}</td>
            <td className={`r num ${signClass(m.trading_pnl_gbp)}`}>{m.trading_pnl_gbp === null ? <span className="text-muted" title={m.trading_status}>not observed</span> : gbp(m.trading_pnl_gbp, 4)}</td>
            <td className="r num">{m.fees_gbp === null ? <span className="text-muted">—</span> : gbp(m.fees_gbp, 4)}</td>
            <td className={`r num ${signClass(m.market_pnl_gbp)}`}>{gbp(m.market_pnl_gbp)}</td>
            <td className="r num">{gbp(m.conversion_costs_gbp)}</td>
            <td className="r num">{gbp(m.ending_value_gbp)}</td>
            <td className="r num">{m.trades}</td>
            <td className="r num" title={`${m.observed_hours} h with fresh order books`}>{m.coverage_pct}%</td>
          </tr>))}
        </tbody>
      </table>
    </Card>
  )
}

function Compare({ r }: { r: any }) {
  const rows = Object.entries(r.variants).filter(([, v]: any) => v.preset === r.variants[r.headline.primary_variant].preset && v.fee_schedule === r.variants[r.headline.primary_variant].fee_schedule)
  const b = r.benchmarks
  return (
    <div className="space-y-4">
      <Card title="Benchmarks and strategies (same contribution dates)" bodyClass="p-0 overflow-auto">
        <table className="data">
          <thead><tr><th>Portfolio</th><th className="r">Contributions</th><th className="r">Ending value</th><th className="r">Net gain</th><th className="r">Trading P&amp;L</th><th className="r">Fees</th><th className="r">XIRR</th><th>Market risk</th><th>Completeness</th></tr></thead>
          <tbody>
            <tr><td>{b.cash.label}</td><td className="r num">{gbp(b.cash.total_contributions_gbp)}</td><td className="r num">{gbp(b.cash.final_value_gbp)}</td><td className="r num">{gbp(b.cash.net_gain_gbp)}</td><td className="r">—</td><td className="r">—</td><td className="r num">0.00%</td><td>{b.cash.market_risk}</td><td><Badge tone="pos">complete</Badge></td></tr>
            <tr><td>{b.btc.label}</td><td className="r num">{gbp(b.btc.total_contributions_gbp)}</td><td className="r num">{gbp(b.btc.final_value_gbp)}</td><td className={`r num ${signClass(b.btc.net_gain_gbp)}`}>{gbp(b.btc.net_gain_gbp)}</td><td className="r">—</td><td className="r num">{gbp(b.btc.fees_gbp)}</td><td className="r num">{b.btc.money_weighted_return_annual_pct?.toFixed(2)}%</td><td>{b.btc.market_risk}</td><td><Badge tone="pos">complete (daily closes)</Badge></td></tr>
            {rows.map(([k, v]: any) => (
              <tr key={k}><td>Arbies {v.label}</td><td className="r num">{gbp(v.summary.total_contributions_gbp)}</td><td className="r num">{gbp(v.summary.final_value_gbp)}</td>
                <td className={`r num ${signClass(v.summary.net_gain_gbp)}`}>{gbp(v.summary.net_gain_gbp)}</td>
                <td className={`r num ${signClass(v.summary.trading.net_trading_profit_gbp)}`}>{gbp(v.summary.trading.net_trading_profit_gbp, 4)}</td>
                <td className="r num">{gbp(v.summary.trading.fees_gbp, 4)}</td>
                <td className="r num">{v.summary.money_weighted_return_annual_pct === null ? '—' : `${v.summary.money_weighted_return_annual_pct.toFixed(2)}%`}</td>
                <td>inventory share of BTC/ETH/USD</td>
                <td><Badge tone={v.summary.result_completeness === 'complete' ? 'pos' : 'warn'}>{v.summary.result_completeness} ({v.summary.trading_coverage_pct}%)</Badge></td></tr>))}
          </tbody>
        </table>
      </Card>
      <Notice tone="sim">Holding Bitcoin carries full BTC price risk. Arbitrage aims to be market-neutral per trade, but the inventory it must hold on each exchange (here {r.variants[r.headline.primary_variant].summary ? 'BTC, ETH and USD' : ''}) still moves with prices. That is the "market / inventory P&amp;L", which is not trading profit.</Notice>
      <Card title="Trading statistics by strategy" bodyClass="p-0 overflow-auto">
        <table className="data">
          <thead><tr><th>Strategy</th><th className="r">Evaluated</th><th className="r">Eligible</th><th className="r">Trades</th><th className="r">Successful</th><th className="r">Failed</th><th className="r">Gross</th><th className="r">Fees</th><th className="r">Slippage</th><th className="r">Rebalancing</th><th className="r">Net</th></tr></thead>
          <tbody>{rows.map(([k, v]: any) => { const t = v.summary.trading; return (
            <tr key={k}><td>{v.label}</td><td className="r num">{t.opportunity_evaluations}</td><td className="r num">{t.opportunities_eligible}</td><td className="r num">{t.trades}</td><td className="r num">{t.successful}</td><td className="r num">{t.failed + t.partial}</td>
              <td className="r num">{gbp(t.gross_trading_profit_gbp, 4)}</td><td className="r num">{gbp(t.fees_gbp, 4)}</td><td className="r num">{gbp(t.slippage_gbp, 4)}</td><td className="r num">{gbp(t.rebalancing_costs_gbp, 4)}</td>
              <td className={`r num ${signClass(t.net_trading_profit_gbp)}`}>{gbp(t.net_trading_profit_gbp, 4)}</td></tr>) })}
          </tbody>
        </table>
      </Card>
      {rows.map(([k, v]: any) => Object.keys(v.breakdown.rejection_reasons ?? {}).length > 0 && (
        <Card key={k} title={`Why ${v.label} did not trade more — rejection reasons`}>
          <KV rows={Object.entries(v.breakdown.rejection_reasons).map(([rk, rv]) => [label(rk), String(rv)])} />
        </Card>))}
    </div>
  )
}

function Study({ r }: { r: any }) {
  const st = r.parameter_study
  return (
    <div className="space-y-4">
      <Card title="Conservative vs aggressive and fee sensitivity (combined strategy)" bodyClass="p-0 overflow-auto">
        {!st ? <Empty>Parameter study not run.</Empty> : (
          <>
            <div className="px-3 py-2 text-[12px] text-muted">{st.selection_rule}. Split at {st.split_at ? dt(st.split_at) : '—'} (train {Math.round(st.train_fraction * 100)}% of observed minutes). Chosen: <b>{st.chosen_preset ?? '—'}</b>. {st.note}</div>
            <table className="data">
              <thead><tr><th>Preset</th><th>Fees</th><th className="r">Train trades</th><th className="r">Train P&amp;L</th><th className="r">Test trades</th><th className="r">Test P&amp;L</th><th className="r">Fees paid</th><th className="r">Ending value</th></tr></thead>
              <tbody>{st.rows.map((x: any) => (
                <tr key={x.name}><td>{x.preset}</td><td>{x.fee_schedule}</td><td className="r num">{x.train_trades}</td><td className="r num">{gbp(x.train_pnl_gbp, 4)}</td><td className="r num">{x.test_trades}</td><td className="r num">{gbp(x.test_pnl_gbp, 4)}</td><td className="r num">{gbp(x.fees_gbp, 4)}</td><td className="r num">{gbp(x.final_value_gbp)}</td></tr>))}
              </tbody>
            </table>
          </>
        )}
      </Card>
      <Card title="Fee schedules used" bodyClass="p-0 overflow-auto">
        <table className="data">
          <thead><tr><th>Schedule</th><th>Exchange</th><th>From</th><th className="r">Maker</th><th className="r">Taker</th><th>Tier</th><th>Verification</th><th>Note</th></tr></thead>
          <tbody>{Object.entries(r.fees).flatMap(([name, sch]: any) => sch.entries.map((e: any, i: number) => (
            <tr key={`${name}${i}`}><td>{name}</td><td>{e.exchange}</td><td>{e.effective_from}</td><td className="r num">{(Number(e.maker) * 100).toFixed(2)}%</td><td className="r num">{(Number(e.taker) * 100).toFixed(2)}%</td><td>{e.tier}</td>
              <td><Badge tone={e.verification.startsWith('official') ? 'pos' : 'warn'}>{e.verification}</Badge></td><td className="max-w-[340px] truncate text-[11px] text-muted" title={e.note}>{e.note}</td></tr>)))}
          </tbody>
        </table>
      </Card>
    </div>
  )
}

function Quality({ r, s }: { r: any; s: any }) {
  const cov = r.coverage
  const sc = r.screening
  return (
    <div className="space-y-4">
      <Card title="Order-book coverage" bodyClass="p-0 overflow-auto">
        <div className="px-3 py-2 text-[12px] text-muted">{cov.definition} Overall: {cov.overlap_minutes} minutes ({cov.overlap_pct}%). Days with order-book data: {r.days_with_order_book_data.join(', ') || 'none'}.</div>
        <table className="data">
          <thead><tr><th>Source</th><th>Exchange</th><th>Pair</th><th className="r">Covered minutes</th><th className="r">%</th><th>First</th><th>Last</th></tr></thead>
          <tbody>{cov.markets.map((m: any, i: number) => (
            <tr key={i}><td>{m.source}</td><td>{m.exchange}</td><td>{m.symbol}</td><td className="r num">{m.covered_minutes}</td><td className="r num">{m.covered_pct}</td><td>{dt(m.first)}</td><td>{dt(m.last)}</td></tr>))}
          </tbody>
        </table>
      </Card>
      <Card title="Candle screening across the whole period" tip={sc.caveat} bodyClass="p-0 overflow-auto">
        <div className="px-3 py-2 text-[12px] text-muted">{sc.caveat}{sc.missing?.length ? ` Missing minute candles: ${sc.missing.join(', ')}.` : ''}</div>
        {sc.pairs.length === 0 ? <Empty>No minute candles fetched.</Empty> : (
          <table className="data">
            <thead><tr><th>Pair</th><th className="r">Minutes compared</th><th className="r">Median |gap|</th><th className="r">p99 |gap|</th><th className="r">Max |gap|</th><th className="r">Round-trip fee (base)</th><th className="r">Minutes above fees: low / base / high</th></tr></thead>
            <tbody>{sc.pairs.map((p: any) => (
              <tr key={p.pair}><td>{p.pair}</td><td className="r num">{p.minutes_both_traded.toLocaleString()}</td><td className="r num">{p.abs_gap_pct_percentiles.p50.toFixed(4)}%</td><td className="r num">{p.abs_gap_pct_percentiles.p99.toFixed(4)}%</td><td className="r num">{p.abs_gap_pct_percentiles.max.toFixed(3)}%</td>
                <td className="r num">{p.round_trip_taker_fee_pct.first?.base?.toFixed(2)}%</td>
                <td className="r num">{['low', 'base', 'high'].map((k) => p.minutes_gap_above_fees[k] ?? '—').join(' / ')}</td></tr>))}
            </tbody>
          </table>
        )}
      </Card>
      {r.warnings.length > 0 && <Notice>{r.warnings.map((w: string, i: number) => <div key={i}>• {w}</div>)}</Notice>}
      <Card title="Assumptions"><ul className="list-disc space-y-1 pl-5 text-[12px]">{r.assumptions.map((a: string, i: number) => <li key={i}>{a}</li>)}</ul></Card>
      <Card title="Reproducibility"><KV rows={[['Result hash', <span className="num text-[11px]">{s.result_hash}</span>], ['Code revision', s.code_revision], ['Events replayed', String(r.events_replayed)]]} /></Card>
    </div>
  )
}

function Ledger({ id, r }: { id: number; r: any }) {
  const [variant, setVariant] = useState(r.headline.primary_variant)
  const trades = useApi<any[]>(`/historical/simulations/${id}/trades?variant=${encodeURIComponent(variant)}&limit=2000`)
  const v = r.variants[variant]
  return (
    <div className="space-y-4">
      <Card title="Downloads" bodyClass="flex flex-wrap gap-2">
        {['summary', 'monthly', 'equity', 'trades'].map((k) => (
          <a key={k} className="inline-flex items-center gap-1.5 rounded-md border border-line bg-panel2 px-2 py-1 text-[12px] font-medium hover:border-accent/60" href={`/api/historical/simulations/${id}/csv?kind=${k}${k === 'summary' ? '' : `&variant=${encodeURIComponent(variant)}`}`}><Download size={13} /> {label(k)} CSV</a>))}
      </Card>
      <Card title="Contributions ledger" bodyClass="p-0 max-h-72 overflow-auto">
        <table className="data">
          <thead><tr><th>Date</th><th>Type</th><th className="r">Amount</th><th className="r">Conversion cost</th><th>Priced with</th></tr></thead>
          <tbody>{v.deposits.map((d: any) => (
            <tr key={d.ts}><td>{d.ts.slice(0, 10)}</td><td>{d.label}</td><td className="r num">{gbp(d.amount_gbp)}</td><td className="r num">{gbp(d.conversion_cost_gbp)}</td><td>{d.priced_with}</td></tr>))}
          </tbody>
        </table>
      </Card>
      <Card title="Trading ledger" bodyClass="p-0 max-h-96 overflow-auto"
        actions={<select value={variant} onChange={(e) => setVariant(e.target.value)}>{Object.keys(r.variants).map((k) => <option key={k} value={k}>{k}</option>)}</select>}>
        {!trades.data ? <Spinner /> : trades.data.length === 0 ? <Empty>No simulated trades. No opportunity cleared fees and risk checks in the observed order-book data.</Empty> : (
          <table className="data">
            <thead><tr><th>Time</th><th>Kind</th><th>Status</th><th>Route</th><th>Exchanges</th><th className="r">Notional</th><th className="r">Fees</th><th className="r">P&amp;L</th></tr></thead>
            <tbody>{trades.data.map((t) => (
              <tr key={t.id}><td>{dt(t.ts)}</td><td>{label(t.kind)}</td><td><Badge tone={t.status === 'success' ? 'pos' : 'warn'}>{t.status}</Badge></td><td>{t.route.join('→')}</td><td>{t.exchanges.join(' / ')}</td>
                <td className="r num">{gbp(t.notional_gbp)}</td><td className="r num">{gbp(t.fees_gbp, 4)}</td><td className={`r num ${signClass(t.pnl_gbp)}`}>{gbp(t.pnl_gbp, 4)}</td></tr>))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}
