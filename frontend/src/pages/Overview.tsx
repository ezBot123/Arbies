import { Link } from 'react-router-dom'
import { PageTitle, useTick } from '../components/Layout'
import { Badge, Card, Empty, Notice, SourceBadge, Spinner, Stat } from '../components/ui'
import { useApi } from '../hooks'
import { ago, gbp, ms, pct, price, signClass } from '../lib/format'

export default function Overview() {
  const { tick } = useTick()
  const status = useApi<any>('/system/status', 5000)
  const events = useApi<any[]>('/system/events?limit=25', 10000)
  const paper = useApi<any>('/paper', 5000)
  const strategies = useApi<any[]>('/strategies')
  const s = status.data
  if (!s) return <Spinner />

  const opps = tick?.opportunities ?? []
  const qualified = opps.filter((o) => o.qualified)
  const exch = Object.values<any>(s.exchanges)
  const healthy = exch.filter((e) => e.healthy).length
  const p = paper.data
  return (
    <div className="space-y-4">
      <PageTitle title="Overview" subtitle={<>System status and headline figures. All P&amp;L shown here is <b>simulated</b> unless marked live.</>} />
      {s.market_data_mode === 'synthetic' && (
        <Notice>Market data mode is <b>SYNTHETIC</b>. Prices are generated for development and do not reflect real markets. Set <code>ARBIES_MARKET_DATA_MODE=live</code> to use real exchange data.</Notice>
      )}
      {s.market_data_mode === 'live' && healthy === 0 && (
        <Notice tone="neg">No exchange feed is currently healthy. Check network access to the exchanges and the System Health agent.</Notice>
      )}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <Stat label="Operating mode" value={s.operating_mode === 'live' ? 'LIVE' : 'Paper'} tone={s.operating_mode === 'live' ? 'text-live' : 'text-sim'} sub={s.live_trading_env_enabled ? 'live enabled in env' : 'live disabled in env'} />
        <Stat label="Exchanges healthy" value={`${healthy}/${exch.length}`} tone={healthy === exch.length ? 'text-pos' : 'text-warn'} sub={`${s.books} order books`} />
        <Stat label="Paper portfolio" value={p?.value_gbp ? gbp(p.value_gbp) : '—'} sub={p?.status ?? 'no session'} tip="simulated" />
        <Stat label="Paper P&L (total)" value={p?.total_pnl_gbp ? gbp(p.total_pnl_gbp) : '—'} tone={signClass(p?.total_pnl_gbp)} sub="simulated, incl. inventory moves" />
        <Stat label="Routes with spread" value={opps.length} sub={`${s.scanner.detected_episodes ?? 0} episodes recorded`} tip="gross_spread" />
        <Stat label="Est. profitable now" value={qualified.length} tone={qualified.length ? 'text-pos' : ''} sub={`${s.scanner.qualified_episodes ?? 0} qualified episodes`} tip="qualified" />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Exchange health" className="min-w-0 xl:col-span-2" bodyClass="p-0 overflow-x-auto">
          <table className="data">
            <thead><tr><th>Exchange</th><th>Status</th><th>Transport</th><th className="r">Updates</th><th className="r">Errors</th><th className="r">Reconnects</th><th className="r">Latency</th><th>Stale / unavailable</th><th>Last error</th></tr></thead>
            <tbody>
              {exch.map((e) => (
                <tr key={e.exchange}>
                  <td className="font-medium">{e.exchange}</td>
                  <td><Badge tone={e.healthy ? 'pos' : 'neg'}>{e.status}</Badge></td>
                  <td>{e.transport}</td>
                  <td className="r num">{e.updates}</td>
                  <td className="r num">{e.errors}</td>
                  <td className="r num">{e.reconnects}</td>
                  <td className="r num">{ms(e.latency_ms_ewma)}</td>
                  <td className="text-warn">{[...e.stale_symbols, ...e.unavailable_symbols].join(', ') || '—'}</td>
                  <td className="max-w-[260px] truncate text-muted" title={e.last_error}>{e.last_error || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Active strategies (simulation)">
          {(strategies.data ?? []).filter((x) => x.enabled_in_simulation).map((x) => (
            <div key={x.name} className="flex items-center justify-between border-b border-line/60 py-1.5 last:border-0">
              <span>{x.name}</span>
              <span className="text-[11px] text-muted">v{x.version} · {x.kind.replace('_', ' ')}</span>
            </div>
          ))}
          {!strategies.data?.some((x) => x.enabled_in_simulation) && <Empty>No strategies enabled.</Empty>}
          <div className="mt-3 grid grid-cols-2 gap-2 text-[12px]">
            <div className="text-muted">Scanner avg</div><div className="num text-right">{s.scanner.avg_scan_ms} ms</div>
            <div className="text-muted">Recorder rows</div><div className="num text-right">{s.recorder?.rows_written ?? 'off'}</div>
            <div className="text-muted">AI provider</div><div className="text-right">{s.ai_provider}</div>
            <div className="text-muted">Code revision</div><div className="num text-right">{s.code_revision}</div>
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Top live opportunities" className="xl:col-span-2" bodyClass="p-0" actions={<Link className="text-[12px] text-accent" to="/scanner">Open scanner →</Link>}>
          {opps.length === 0 ? <Empty>No positive spreads right now.</Empty> : (
            <table className="data">
              <thead><tr><th>Route</th><th>Kind</th><th className="r">Gross</th><th className="r">Est. net</th><th className="r">Est. net £</th><th>Status</th></tr></thead>
              <tbody>
                {opps.slice(0, 8).map((o) => (
                  <tr key={o.key}>
                    <td>{o.kind === 'cross_exchange' ? `${o.symbols[0]} ${o.buy_exchange} → ${o.sell_exchange}` : `${o.exchanges[0]}: ${o.route.join('→')}`}</td>
                    <td className="text-muted">{o.kind === 'cross_exchange' ? 'cross' : 'triangular'}</td>
                    <td className="r num">{pct(o.gross_spread_pct)}</td>
                    <td className={`r num ${signClass(o.net_return_pct)}`}>{pct(o.net_return_pct)}</td>
                    <td className={`r num ${signClass(o.net_profit_gbp)}`}>{gbp(o.net_profit_gbp)}</td>
                    <td>{o.qualified ? <Badge tone="pos">qualified</Badge> : <Badge>{o.rejection_reasons[0]?.replace(/_/g, ' ')}</Badge>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        <Card title="Recent system events" bodyClass="max-h-80 overflow-auto p-0">
          {(events.data ?? []).length === 0 ? <Empty>No events yet.</Empty> : (
            <ul className="divide-y divide-line/60 text-[12px]">
              {events.data!.map((e, i) => (
                <li key={i} className="px-3 py-1.5">
                  <div className="flex justify-between gap-2">
                    <span className={e.level === 'critical' || e.level === 'error' ? 'text-neg' : e.level === 'warning' ? 'text-warn' : 'text-muted'}>{e.source}</span>
                    <span className="text-muted">{ago(e.ts)}</span>
                  </div>
                  <div className="truncate" title={e.message}>{e.message}</div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card title="Top of book" bodyClass="p-0" actions={<SourceBadge source={tick?.data_source} />}>
        <table className="data">
          <thead><tr><th>Exchange</th><th>Symbol</th><th className="r">Bid</th><th className="r">Ask</th><th className="r">Spread</th><th className="r">Age</th></tr></thead>
          <tbody>
            {(tick?.tops ?? []).map((t) => {
              const b = Number(t.bid), a = Number(t.ask)
              return (
                <tr key={t.exchange + t.symbol}>
                  <td>{t.exchange}</td><td className="font-medium">{t.symbol}</td>
                  <td className="r num text-pos">{price(t.bid)}</td><td className="r num text-neg">{price(t.ask)}</td>
                  <td className="r num">{a ? pct(((a - b) / a) * 100) : '—'}</td>
                  <td className={`r num ${t.age_ms > 5000 ? 'text-warn' : ''}`}>{ms(t.age_ms)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </Card>
    </div>
  )
}
