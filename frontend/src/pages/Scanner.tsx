import { ArrowDown, ArrowUp } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { Opportunity } from '../api'
import { PageTitle, useTick } from '../components/Layout'
import { Badge, Card, Empty, KV, Modal, SourceBadge, Tip } from '../components/ui'
import { useApi } from '../hooks'
import { gbp, label, money, ms, num, pct, price, signClass, toNum } from '../lib/format'

type SortKey = 'net_return_pct' | 'gross_spread_pct' | 'net_profit_gbp' | 'age_ms' | 'notional_gbp'

const REASONS = [
  'spread_non_positive', 'fees_exceed_profit', 'below_threshold', 'insufficient_liquidity', 'insufficient_inventory',
  'below_min_order', 'stale_data', 'exchange_unavailable', 'no_valuation',
]

export default function Scanner() {
  const { tick } = useTick()
  const meta = useApi<any>('/opportunities?limit=1', 15000)
  const [kind, setKind] = useState('all')
  const [qualifiedOnly, setQualifiedOnly] = useState(false)
  const [exchange, setExchange] = useState('all')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<{ k: SortKey; desc: boolean }>({ k: 'net_return_pct', desc: true })
  const [selected, setSelected] = useState<Opportunity | null>(null)

  const all = useMemo(() => tick?.opportunities ?? [], [tick])
  const rows = useMemo(() => {
    let r = all.filter((o) => (kind === 'all' || o.kind === kind) && (!qualifiedOnly || o.qualified)
      && (exchange === 'all' || o.exchanges.includes(exchange))
      && (!search || [...o.symbols, ...o.route].join(' ').toLowerCase().includes(search.toLowerCase())))
    r = [...r].sort((a, b) => {
      const av = toNum(a[sort.k] as any) ?? -Infinity, bv = toNum(b[sort.k] as any) ?? -Infinity
      return sort.desc ? bv - av : av - bv
    })
    return r
  }, [all, kind, qualifiedOnly, exchange, search, sort])

  const reasonCounts = useMemo(() => {
    const c: Record<string, number> = {}
    all.forEach((o) => o.rejection_reasons.forEach((r) => (c[r] = (c[r] ?? 0) + 1)))
    return c
  }, [all])
  const exchanges = [...new Set(all.flatMap((o) => o.exchanges))].sort()
  const params = meta.data?.params
  const explanations: Record<string, string> = meta.data?.explanations ?? {}

  const H = ({ k, children, tip }: { k: SortKey; children: React.ReactNode; tip?: string }) => (
    <th className="r cursor-pointer select-none" onClick={() => setSort((s) => ({ k, desc: s.k === k ? !s.desc : true }))}>
      <span className="inline-flex items-center gap-1">{children}{tip && <Tip k={tip} />}{sort.k === k && (sort.desc ? <ArrowDown size={11} /> : <ArrowUp size={11} />)}</span>
    </th>
  )

  return (
    <div className="space-y-4">
      <PageTitle
        title="Opportunity Scanner"
        subtitle={<>Live evaluation of every route with a positive gross spread, using executable depth-walked prices. Profit figures are <b>estimates before execution</b>, never realised results.</>}
        actions={<SourceBadge source={tick?.data_source} />}
      />
      {params && (
        <div className="flex flex-wrap gap-2 text-[11px] text-muted">
          <span>Threshold <b className="text-fg">{params.min_net_profit_pct}%</b></span>·
          <span>Max size <b className="text-fg">£{params.max_trade_gbp}</b></span>·
          <span>Max data age <b className="text-fg">{params.max_data_age_ms} ms</b></span>·
          <span>Slippage buffer <b className="text-fg">{params.slippage_buffer_bps} bps</b></span>·
          <span>Latency cost <b className="text-fg">{params.latency_cost_bps} bps/leg</b></span>·
          <span>Rebalancing <b className="text-fg">{params.rebalancing_cost_bps} bps</b></span>·
          <span>Leg failure <b className="text-fg">{(Number(params.leg_failure_prob) * 100).toFixed(1)}% × {params.leg_failure_loss_bps} bps</b></span>
          <span className="ml-1">(change in Settings)</span>
        </div>
      )}
      <Card bodyClass="flex flex-wrap items-center gap-3">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="all">All strategies</option><option value="cross_exchange">Cross-exchange</option><option value="triangular">Triangular</option>
        </select>
        <select value={exchange} onChange={(e) => setExchange(e.target.value)}>
          <option value="all">All exchanges</option>{exchanges.map((e) => <option key={e}>{e}</option>)}
        </select>
        <input placeholder="Filter pair / asset…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <label className="flex items-center gap-1.5 text-[12px]"><input type="checkbox" checked={qualifiedOnly} onChange={(e) => setQualifiedOnly(e.target.checked)} /> Qualified only</label>
        <span className="ml-auto text-[12px] text-muted">{rows.length} of {all.length} routes · scan {tick?.scanner?.last_scan_ms ?? '—'} ms</span>
      </Card>

      <Card title="Why opportunities are rejected" bodyClass="flex flex-wrap gap-2">
        {REASONS.filter((r) => reasonCounts[r]).map((r) => (
          <span key={r} title={explanations[r]} className="cursor-help"><Badge tone={r === 'stale_data' || r === 'exchange_unavailable' ? 'neg' : 'muted'}>{label(r)}: {reasonCounts[r]}</Badge></span>
        ))}
        {!Object.keys(reasonCounts).length && <span className="text-[12px] text-muted">No rejections in the current set.</span>}
      </Card>

      <Card bodyClass="p-0 max-h-[60vh] overflow-auto">
        {rows.length === 0 ? <Empty>No routes match. Spreads appear here when a buy price on one venue is below a sell price elsewhere.</Empty> : (
          <table className="data">
            <thead>
              <tr>
                <th>Route</th><th>Buy</th><th>Sell</th>
                <H k="gross_spread_pct" tip="gross_spread">Gross</H>
                <th className="r">Fees <Tip k="fees" /></th>
                <th className="r">Slippage <Tip k="slippage" /></th>
                <th className="r">Other costs</th>
                <H k="net_return_pct" tip="net_return">Est. net %</H>
                <H k="net_profit_gbp" tip="net_profit">Est. net £</H>
                <H k="notional_gbp" tip="max_size">Size £</H>
                <H k="age_ms" tip="age">Age</H>
                <th className="r">Conf. <Tip k="confidence" /></th>
                <th>Status <Tip k="qualified" /></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((o) => {
                const other = (toNum(o.latency_cost) ?? 0) + (toNum(o.rebalancing_cost) ?? 0) + (toNum(o.fx_cost) ?? 0) + (toNum(o.leg_failure_cost) ?? 0)
                return (
                  <tr key={o.key} className="cursor-pointer" onClick={() => setSelected(o)}>
                    <td className="font-medium">{o.kind === 'cross_exchange' ? o.symbols.join(' / ') : o.route.join('→')}<span className="ml-1.5 text-[11px] text-muted">{o.kind === 'triangular' ? 'tri' : 'cross'}</span></td>
                    <td>{o.buy_exchange ?? o.exchanges[0]}</td>
                    <td>{o.sell_exchange ?? o.exchanges[0]}</td>
                    <td className="r num">{pct(o.gross_spread_pct)}</td>
                    <td className="r num">{money(o.fees, o.profit_currency)}</td>
                    <td className="r num">{money(o.slippage_buffer, o.profit_currency)}</td>
                    <td className="r num">{money(other, o.profit_currency)}</td>
                    <td className={`r num font-medium ${signClass(o.net_return_pct)}`}>{pct(o.net_return_pct)}</td>
                    <td className={`r num ${signClass(o.net_profit_gbp)}`}>{gbp(o.net_profit_gbp)}</td>
                    <td className="r num">{gbp(o.notional_gbp)}</td>
                    <td className="r num">{ms(o.age_ms)}</td>
                    <td className={`r num ${o.confidence < 0.3 ? 'text-warn' : ''}`}>{o.confidence.toFixed(2)}</td>
                    <td>{o.qualified ? <Badge tone="pos">qualified</Badge> : <span title={o.rejection_explanations.join('\n')}><Badge>{label(o.rejection_reasons[0] ?? 'rejected')}</Badge></span>}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>
      <OpportunityDetail opp={selected} onClose={() => setSelected(null)} />
    </div>
  )
}

function OpportunityDetail({ opp, onClose }: { opp: Opportunity | null; onClose: () => void }) {
  if (!opp) return null
  const c = opp.profit_currency
  return (
    <Modal open onClose={onClose} title={opp.kind === 'cross_exchange' ? `${opp.symbols.join('/')} · ${opp.buy_exchange} → ${opp.sell_exchange}` : `${opp.exchanges[0]} · ${opp.route.join(' → ')}`} wide>
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <h4 className="mb-2 text-[12px] font-semibold uppercase text-muted">Estimated profit breakdown ({c})</h4>
          <KV rows={[
            [<>Trade notional</>, money(opp.notional, c)],
            [<>Gross profit (depth-walked) <Tip k="gross_profit" /></>, money(opp.gross_profit, c)],
            [<>− Fees <Tip k="fees" /></>, money(opp.fees, c)],
            [<>− Slippage buffer <Tip k="slippage" /></>, money(opp.slippage_buffer, c)],
            [<>− Latency cost <Tip k="latency_cost" /></>, money(opp.latency_cost, c)],
            [<>− Rebalancing cost <Tip k="rebalancing_cost" /></>, money(opp.rebalancing_cost, c)],
            [<>− FX cost</>, money(opp.fx_cost, c)],
            [<>− Expected leg-failure cost <Tip k="leg_failure_cost" /></>, money(opp.leg_failure_cost, c)],
            [<b>= Estimated net profit</b>, <b className={signClass(opp.net_profit)}>{money(opp.net_profit, c)} ({gbp(opp.net_profit_gbp)})</b>],
            [<>Estimated net return</>, pct(opp.net_return_pct)],
            [<>Depth slippage (included in gross)</>, money(opp.depth_slippage, c)],
            [<>Max executable size</>, num(opp.max_size_base, 8)],
            [<>Data age / confidence</>, `${ms(opp.data_age_ms)} / ${opp.confidence.toFixed(2)}`],
          ]} />
        </div>
        <div className="space-y-3">
          <h4 className="text-[12px] font-semibold uppercase text-muted">Legs</h4>
          <table className="data">
            <thead><tr><th>#</th><th>Venue</th><th>Side</th><th className="r">Amount</th><th className="r">VWAP</th><th className="r">Top</th><th className="r">Levels</th></tr></thead>
            <tbody>
              {opp.legs.map((l, i) => (
                <tr key={i}><td>{i + 1}</td><td>{l.exchange} {l.symbol}</td><td className={l.side === 'buy' ? 'text-pos' : 'text-neg'}>{l.side}</td>
                  <td className="r num">{num(l.base_amount, 8)}</td><td className="r num">{price(l.vwap)}</td><td className="r num">{price(l.top_price)}</td><td className="r num">{l.levels_used}</td></tr>
              ))}
            </tbody>
          </table>
          {!opp.qualified && (
            <div className="space-y-1">
              <h4 className="text-[12px] font-semibold uppercase text-muted">Why rejected</h4>
              {opp.rejection_explanations.map((e, i) => <div key={i} className="text-[12px] text-warn">• {e}</div>)}
            </div>
          )}
          <p className="text-[11px] text-muted">Never risk-free: legs execute independently and can fail. The leg-failure cost is a modelled expectation, not a bound on losses.</p>
        </div>
      </div>
    </Modal>
  )
}
