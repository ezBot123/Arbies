import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'
import { EquityChart } from '../components/charts'
import { PageTitle } from '../components/Layout'
import { Badge, Card, Empty, Notice, Spinner, Stat } from '../components/ui'
import { useApi } from '../hooks'
import { SERIES, usePalette } from '../lib/colors'
import { gbp, num, signClass } from '../lib/format'

function Donut({ data }: { data: { name: string; value: number }[] }) {
  const p = usePalette()
  if (!data.length) return <Empty>No balances.</Empty>
  return (
    <ResponsiveContainer width="100%" height={200}>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" innerRadius={50} outerRadius={80} isAnimationActive={false} label={(d: any) => d.name} fontSize={11}>
          {data.map((_, i) => <Cell key={i} fill={SERIES[i % SERIES.length]} stroke={p.panel} />)}
        </Pie>
        <Tooltip contentStyle={{ background: p.panel, border: `1px solid ${p.line}`, fontSize: 12 }} formatter={(v: any) => `£${Number(v).toFixed(2)}`} />
      </PieChart>
    </ResponsiveContainer>
  )
}

export default function Portfolio() {
  const pf = useApi<any>('/portfolio', 4000)
  const equity = useApi<any[]>('/paper/equity', 15000)
  if (!pf.data) return <Spinner />
  const s = pf.data.paper
  const byEx = Object.entries(s.value_by_exchange ?? {}).map(([name, v]) => ({ name, value: Number(v) }))
  const byAsset = Object.entries(s.value_by_asset ?? {}).map(([name, v]) => ({ name, value: Number(v) }))
  const assets = [...new Set((s.balances ?? []).map((b: any) => b.asset))] as string[]
  const exchanges = [...new Set((s.balances ?? []).map((b: any) => b.exchange))] as string[]
  const bal = (ex: string, a: string) => (s.balances ?? []).find((b: any) => b.exchange === ex && b.asset === a)
  return (
    <div className="space-y-4">
      <PageTitle title="Portfolio" subtitle="Virtual paper-trading inventory across exchanges." actions={<Badge tone="sim">VIRTUAL BALANCES</Badge>} />
      <Notice tone="sim">{pf.data.note}</Notice>
      {!s.balances ? <Card><Empty>No paper session. Start one on the Paper Trading page to create a virtual portfolio.</Empty></Card> : (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Portfolio value" value={gbp(s.value_gbp)} sub={s.valuation_estimated ? 'includes fallback FX estimate' : 'mid-price valuation'} />
            <Stat label="Initial value" value={gbp(s.initial_value_gbp)} />
            <Stat label="Unrealised + realised change" value={gbp(s.total_pnl_gbp)} tone={signClass(s.total_pnl_gbp)} sub="simulated" />
            <Stat label="Crypto exposure" value={gbp(s.exposure?.total_gbp)} />
          </div>
          <div className="grid gap-4 lg:grid-cols-3">
            <Card title="By exchange"><Donut data={byEx} /></Card>
            <Card title="By asset"><Donut data={byAsset} /></Card>
            <Card title="Rebalancing requirements">
              {pf.data.rebalancing_needs.length === 0 ? <Empty>Inventory is reasonably distributed.</Empty> : pf.data.rebalancing_needs.map((r: any) => (
                <div key={r.asset} className="mb-2 text-[12px]"><b>{r.asset}</b>: {Object.entries(r.shares_pct).map(([e, v]: any) => `${e} ${v.toFixed(0)}%`).join(' · ')}<div className="text-muted">{r.note}</div></div>
              ))}
              {s.in_transit?.length > 0 && <div className="mt-2 text-[12px]"><div className="text-muted">In transit</div>{s.in_transit.map((t: any, i: number) => <div key={i}>{num(t.amount)} {t.asset} → {t.exchange}</div>)}</div>}
            </Card>
          </div>
          <Card title="Inventory distribution" bodyClass="p-0">
            <table className="data">
              <thead><tr><th>Asset</th>{exchanges.map((e) => <th key={e} className="r">{e}</th>)}<th className="r">Total value</th></tr></thead>
              <tbody>{assets.map((a) => (
                <tr key={a}><td className="font-medium">{a}</td>
                  {exchanges.map((e) => <td key={e} className="r num">{bal(e, a) ? num(bal(e, a).amount, 8) : '—'}</td>)}
                  <td className="r num">{gbp(s.value_by_asset?.[a])}</td></tr>
              ))}</tbody>
            </table>
          </Card>
          <Card title="Historical value (simulated)">{(equity.data ?? []).length > 1 ? <EquityChart series={[{ name: 'portfolio', points: equity.data!.map((p) => ({ ts: p.ts, value: Number(p.value_gbp) })) }]} /> : <Empty>Not enough history yet.</Empty>}</Card>
        </>
      )}
      <Card title="Live exchange accounts (read-only)">
        {pf.data.live_accounts.length === 0 ? <Empty>No exchange credentials stored. Live balances are not shown.</Empty> : pf.data.live_accounts.map((a: any) => (
          <div key={a.exchange} className="text-[12px]">{a.exchange} · {a.permissions} · key …{a.key_fingerprint} {a.sandbox && <Badge>sandbox</Badge>}</div>
        ))}
      </Card>
    </div>
  )
}
