import { useQueryClient } from '@tanstack/react-query'
import { History, Plus, Save } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { post, put } from '../api'
import { PageTitle } from '../components/Layout'
import { Badge, Button, Card, Empty, ErrorBox, Field, Modal, Spinner } from '../components/ui'
import { useApi } from '../hooks'
import { dt, gbp, signClass } from '../lib/format'

const PARAM_FIELDS: [string, string][] = [
  ['min_net_profit_pct', 'Min est. net profit (%)'], ['max_trade_gbp', 'Max trade (£)'], ['max_data_age_ms', 'Max data age (ms)'],
  ['slippage_buffer_bps', 'Slippage buffer (bps)'], ['latency_ms', 'Latency (ms)'], ['latency_cost_bps', 'Latency cost (bps/leg)'],
  ['rebalancing_cost_bps', 'Rebalancing cost (bps)'], ['fx_cost_bps', 'FX cost (bps)'], ['leg_failure_prob', 'Leg failure probability'],
  ['leg_failure_loss_bps', 'Leg failure loss (bps)'],
]

export default function Strategies() {
  const qc = useQueryClient()
  const list = useApi<any[]>('/strategies')
  const runs = useApi<any[]>('/backtests')
  const [edit, setEdit] = useState<any>(null)
  const [history, setHistory] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const nav = useNavigate()
  if (!list.data) return <Spinner />
  const toggle = async (s: any) => { await post(`/strategies/${encodeURIComponent(s.name)}/enabled`, { enabled: !s.enabled_in_simulation }); qc.invalidateQueries({ queryKey: ['/strategies'] }) }
  return (
    <div className="space-y-4">
      <PageTitle title="Strategy Manager" subtitle="Versioned strategy configurations. Every save creates a new immutable version; previous versions can be restored. Enabling affects simulation only."
                 actions={<Button variant="primary" onClick={() => setCreating(true)}><Plus size={14} /> New strategy</Button>} />
      <div className="grid gap-3 lg:grid-cols-2">
        {list.data.map((s) => {
          const related = (runs.data ?? []).filter((r) => r.strategy_name === s.name && r.status === 'completed')
          return (
            <Card key={s.name} title={<span className="normal-case">{s.name} <span className="text-muted">v{s.version}</span></span>}
                  actions={<>
                    <Badge tone={s.kind === 'triangular' ? 'sim' : 'accent'}>{s.kind.replace('_', ' ')}</Badge>
                    <Button size="sm" variant={s.enabled_in_simulation ? 'success' : 'default'} onClick={() => toggle(s)}>{s.enabled_in_simulation ? 'Enabled (sim)' : 'Disabled'}</Button>
                  </>}>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[12px]">
                {PARAM_FIELDS.slice(0, 6).map(([k, l]) => <div key={k} className="contents"><span className="text-muted">{l}</span><span className="num text-right">{String(s.params.strategy[k])}</span></div>)}
                <span className="text-muted">Exchanges</span><span className="text-right">{s.params.exchanges.join(', ') || 'all'}</span>
                <span className="text-muted">Pairs</span><span className="text-right">{s.params.symbols.join(', ') || 'all'}</span>
              </div>
              {s.note && <p className="mt-2 text-[11px] text-muted">{s.note}</p>}
              <div className="mt-2 text-[11px] text-muted">Backtests linked: {related.length}{related[0] && <> · latest P&amp;L <span className={signClass(related[0].headline?.total_pnl_gbp)}>{gbp(related[0].headline?.total_pnl_gbp)}</span></>}</div>
              <div className="mt-3 flex gap-2">
                <Button size="sm" onClick={() => setEdit(s)}><Save size={12} /> Edit (new version)</Button>
                <Button size="sm" variant="ghost" onClick={() => setHistory(s.name)}><History size={12} /> Versions</Button>
                <Button size="sm" variant="ghost" onClick={() => nav('/backtests')}>Backtest →</Button>
              </div>
            </Card>
          )
        })}
      </div>
      {edit && <EditModal s={edit} onClose={() => { setEdit(null); qc.invalidateQueries({ queryKey: ['/strategies'] }) }} />}
      {creating && <EditModal onClose={() => { setCreating(false); qc.invalidateQueries({ queryKey: ['/strategies'] }) }} />}
      {history && <HistoryModal name={history} onClose={() => { setHistory(null); qc.invalidateQueries({ queryKey: ['/strategies'] }) }} />}
    </div>
  )
}

function EditModal({ s, onClose }: { s?: any; onClose: () => void }) {
  const [name, setName] = useState(s?.name ?? '')
  const [kind, setKind] = useState(s?.kind ?? 'cross_exchange')
  const [params, setParams] = useState<Record<string, any>>(s?.params.strategy ?? { min_net_profit_pct: '0.30', max_trade_gbp: '25', max_data_age_ms: 5000, slippage_buffer_bps: '5', latency_ms: 250, latency_cost_bps: '2', rebalancing_cost_bps: '10', fx_cost_bps: '10', leg_failure_prob: '0.02', leg_failure_loss_bps: '50' })
  const [exchanges, setExchanges] = useState((s?.params.exchanges ?? []).join(','))
  const [symbols, setSymbols] = useState((s?.params.symbols ?? []).join(','))
  const [note, setNote] = useState('')
  const [error, setError] = useState<unknown>(null)
  const save = async () => {
    const body = { strategy: params, exchanges: exchanges.split(',').map((x: string) => x.trim()).filter(Boolean), symbols: symbols.split(',').map((x: string) => x.trim()).filter(Boolean) }
    try {
      if (s) await put(`/strategies/${encodeURIComponent(s.name)}`, { params: body, note })
      else await post('/strategies', { name, kind, params: body, note })
      onClose()
    } catch (e) { setError(e) }
  }
  return (
    <Modal open onClose={onClose} title={s ? `Edit ${s.name} (creates v${s.version + 1})` : 'New strategy'} wide>
      <div className="grid gap-3 md:grid-cols-3">
        {!s && <Field label="Name"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>}
        {!s && <Field label="Kind"><select value={kind} onChange={(e) => setKind(e.target.value)}><option value="cross_exchange">Cross-exchange</option><option value="triangular">Triangular</option></select></Field>}
        {PARAM_FIELDS.map(([k, l]) => <Field key={k} label={l}><input value={params[k] ?? ''} onChange={(e) => setParams({ ...params, [k]: e.target.value })} /></Field>)}
        <Field label="Exchanges (empty = all)"><input value={exchanges} onChange={(e) => setExchanges(e.target.value)} /></Field>
        <Field label="Pairs (empty = all)"><input value={symbols} onChange={(e) => setSymbols(e.target.value)} /></Field>
        <Field label="Change note"><input value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      </div>
      <div className="mt-3"><ErrorBox error={error} /></div>
      <div className="mt-3 flex justify-end gap-2"><Button onClick={onClose}>Cancel</Button><Button variant="primary" onClick={save}>Save</Button></div>
    </Modal>
  )
}

function HistoryModal({ name, onClose }: { name: string; onClose: () => void }) {
  const qc = useQueryClient()
  const v = useApi<any[]>(`/strategies/${encodeURIComponent(name)}/versions`)
  const restore = async (ver: number) => { await post(`/strategies/${encodeURIComponent(name)}/restore/${ver}`); qc.invalidateQueries() }
  return (
    <Modal open onClose={onClose} title={`${name} — version history`} wide>
      {!v.data ? <Spinner /> : !v.data.length ? <Empty>No versions</Empty> : (
        <table className="data">
          <thead><tr><th>Version</th><th>Created</th><th>By</th><th>Note</th><th>Key params</th><th></th></tr></thead>
          <tbody>{v.data.map((x) => (
            <tr key={x.version}>
              <td>v{x.version} {x.is_current && <Badge tone="pos">current</Badge>}</td><td className="text-[11px]">{dt(x.created_at)}</td><td>{x.created_by}</td>
              <td className="max-w-[200px] truncate">{x.note}</td>
              <td className="text-[11px]">min {x.params.strategy.min_net_profit_pct}% · max £{x.params.strategy.max_trade_gbp} · slip {x.params.strategy.slippage_buffer_bps}bps</td>
              <td>{!x.is_current && <Button size="sm" onClick={() => restore(x.version)}>Restore</Button>}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </Modal>
  )
}
