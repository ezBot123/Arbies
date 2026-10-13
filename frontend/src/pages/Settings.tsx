import { useQueryClient } from '@tanstack/react-query'
import { ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { del, post, put } from '../api'
import { PageTitle } from '../components/Layout'
import { Badge, Button, Card, Empty, ErrorBox, Field, KV, Modal, Notice, Spinner, Tabs } from '../components/ui'
import { useApi } from '../hooks'
import { dt, label } from '../lib/format'

export default function SettingsPage() {
  const s = useApi<any>('/settings')
  const [tab, setTab] = useState('exchanges')
  if (!s.data) return <Spinner />
  return (
    <div className="space-y-4">
      <PageTitle title="System Settings" subtitle="Secrets are configured through backend environment variables and are never displayed here." />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: 'exchanges', label: 'Exchange connections' }, { id: 'scanner', label: 'Strategy defaults' }, { id: 'risk', label: 'Risk parameters' },
        { id: 'ai', label: 'AI providers' }, { id: 'data', label: 'Data retention' }, { id: 'mode', label: 'Operating mode' }, { id: 'security', label: 'Security & audit' },
      ]} />
      {tab === 'exchanges' && <Exchanges data={s.data} />}
      {tab === 'scanner' && <ScannerSettings data={s.data} />}
      {tab === 'risk' && <RiskSettings data={s.data} />}
      {tab === 'ai' && <AiSettings data={s.data} />}
      {tab === 'data' && <DataSettings data={s.data} />}
      {tab === 'mode' && <ModeSettings data={s.data} />}
      {tab === 'security' && <Security data={s.data} />}
    </div>
  )
}

function Exchanges({ data }: { data: any }) {
  const qc = useQueryClient()
  const ex = useApi<any[]>('/exchanges', 5000)
  const [form, setForm] = useState({ exchange: data.settings.exchanges[0] ?? 'kraken', api_key: '', secret: '', password: '', permissions: 'read_only', sandbox: true, admin_password: '' })
  const [error, setError] = useState<unknown>(null)
  const [balances, setBalances] = useState<any>(null)
  const save = async () => {
    setError(null)
    try { await post('/credentials', { ...form, password: form.password || null }); setForm({ ...form, api_key: '', secret: '', password: '', admin_password: '' }); qc.invalidateQueries() } catch (e) { setError(e) }
  }
  const fetchBal = async (exchange: string) => { setError(null); try { setBalances(await (await import('../api')).api(`/live/balances/${exchange}`)) } catch (e) { setError(e) } }
  return (
    <div className="space-y-4">
      <Card title="Market data connections" bodyClass="p-0">
        <table className="data">
          <thead><tr><th>Exchange</th><th>Status</th><th>Transport</th><th>Symbols</th><th>Fee schedule</th><th>UK eligibility</th></tr></thead>
          <tbody>{(ex.data ?? []).map((e) => (
            <tr key={e.exchange}>
              <td className="font-medium">{e.exchange}</td><td><Badge tone={e.healthy ? 'pos' : 'neg'}>{e.status}</Badge></td><td>{e.transport}</td>
              <td className="max-w-[200px] truncate" title={e.symbols.join(', ')}>{e.symbols.length} ({e.unavailable_symbols.length} unavailable)</td>
              <td className="text-[11px]">{Object.entries(e.fees).slice(0, 1).map(([sym, f]: any) => `${sym}: taker ${(f.taker * 100).toFixed(2)}% (${f.source})`)}</td>
              <td className="max-w-[320px] whitespace-normal text-[11px] text-muted">{e.uk_eligibility_note}</td>
            </tr>
          ))}</tbody>
        </table>
        <p className="p-3 text-[11px] text-muted">Market data uses public endpoints only (no credentials). Configure exchanges and pairs with ARBIES_EXCHANGES and ARBIES_SYMBOLS. Fee schedules are the public base tier unless account credentials are added.</p>
      </Card>
      <Card title="Exchange API credentials (stored encrypted on the backend)">
        <Notice tone="accent">Use <b>read-only</b> API keys with <b>withdrawals disabled</b>. Arbies never calls withdrawal or transfer endpoints. Keys are encrypted with ARBIES_SECRET_KEY and are never returned to the browser.</Notice>
        {data.credentials.length > 0 && (
          <table className="data my-3"><thead><tr><th>Exchange</th><th>Permissions</th><th>Environment</th><th>Key fingerprint</th><th>Added</th><th></th></tr></thead>
            <tbody>{data.credentials.map((c: any) => (
              <tr key={c.exchange}><td>{c.exchange}</td><td><Badge tone={c.permissions === 'trade' ? 'warn' : 'muted'}>{c.permissions}</Badge></td><td>{c.sandbox ? 'sandbox' : 'production'}</td><td className="num">{c.key_fingerprint}</td><td className="text-[11px]">{dt(c.created_at)}</td>
                <td className="flex gap-1"><Button size="sm" onClick={() => fetchBal(c.exchange)}>Read balances</Button><Button size="sm" variant="danger" onClick={async () => { await del(`/credentials/${c.exchange}`); qc.invalidateQueries() }}>Remove</Button></td></tr>
            ))}</tbody></table>
        )}
        {balances && <div className="mb-3 rounded border border-line p-2 text-[12px]"><b>{balances.exchange}</b> (read-only): {Object.entries(balances.balances).map(([k, v]) => `${k} ${v}`).join(' · ') || 'no balances'}</div>}
        <div className="mt-3 grid gap-2 md:grid-cols-4">
          <Field label="Exchange"><select value={form.exchange} onChange={(e) => setForm({ ...form, exchange: e.target.value })}>{data.settings.exchanges.map((x: string) => <option key={x}>{x}</option>)}</select></Field>
          <Field label="API key"><input value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} autoComplete="off" /></Field>
          <Field label="API secret"><input type="password" value={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.value })} autoComplete="off" /></Field>
          <Field label="Passphrase (if required)"><input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="off" /></Field>
          <Field label="Permissions"><select value={form.permissions} onChange={(e) => setForm({ ...form, permissions: e.target.value })}><option value="read_only">Read-only (recommended)</option><option value="trade">Trade (required for live mode)</option></select></Field>
          <Field label="Environment"><select value={form.sandbox ? 'sandbox' : 'production'} onChange={(e) => setForm({ ...form, sandbox: e.target.value === 'sandbox' })}><option value="sandbox">Sandbox / testnet</option><option value="production">Production</option></select></Field>
          <Field label="Your Arbies password"><input type="password" value={form.admin_password} onChange={(e) => setForm({ ...form, admin_password: e.target.value })} /></Field>
          <div className="flex items-end"><Button variant="primary" onClick={save} disabled={!data.settings.credential_encryption_configured}>Store credentials</Button></div>
        </div>
        {!data.settings.credential_encryption_configured && <p className="mt-2 text-[11px] text-warn">ARBIES_SECRET_KEY is not set on the backend, so credentials cannot be stored.</p>}
        <div className="mt-2"><ErrorBox error={error} /></div>
      </Card>
    </div>
  )
}

const SCANNER_FIELDS: [string, string][] = [
  ['min_net_profit_pct', 'Min est. net profit (%)'], ['max_trade_gbp', 'Max trade size (£)'], ['max_data_age_ms', 'Max data age (ms)'],
  ['slippage_buffer_bps', 'Slippage buffer (bps)'], ['latency_ms', 'Expected latency (ms)'], ['latency_cost_bps', 'Latency cost (bps/leg)'],
  ['rebalancing_cost_bps', 'Rebalancing cost (bps)'], ['fx_cost_bps', 'FX cost (bps)'], ['leg_failure_prob', 'Leg failure probability'], ['leg_failure_loss_bps', 'Leg failure loss (bps)'],
]

function ScannerSettings({ data }: { data: any }) {
  const qc = useQueryClient()
  const [p, setP] = useState<any>(data.scanner_params)
  const [enabled, setEnabled] = useState<any>(data.scanner_enabled)
  const [starts, setStarts] = useState((data.scanner_params.start_currencies ?? []).join(','))
  const [equiv, setEquiv] = useState((data.scanner_params.quote_equivalents ?? []).map((g: string[]) => g.join('=')).join(';'))
  const [msg, setMsg] = useState<unknown>(null)
  const save = async () => {
    try {
      await put('/settings/scanner', { params: { ...p, start_currencies: starts.split(',').map((x: string) => x.trim()).filter(Boolean),
        quote_equivalents: equiv.split(';').map((g: string) => g.split('=').map((x) => x.trim()).filter(Boolean)).filter((g: string[]) => g.length > 1) }, enabled })
      setMsg(null); qc.invalidateQueries()
    } catch (e) { setMsg(e) }
  }
  return (
    <Card title="Live scanner parameters" bodyClass="space-y-3">
      <p className="text-[12px] text-muted">These parameters drive the live Opportunity Scanner. Paper sessions and backtests carry their own copies so results stay reproducible.</p>
      <div className="flex gap-4">{Object.keys(enabled).map((k) => <label key={k} className="flex items-center gap-1.5"><input type="checkbox" checked={enabled[k]} onChange={(e) => setEnabled({ ...enabled, [k]: e.target.checked })} />{label(k)}</label>)}</div>
      <div className="grid gap-3 md:grid-cols-4">
        {SCANNER_FIELDS.map(([k, l]) => <Field key={k} label={l}><input value={p[k] ?? ''} onChange={(e) => setP({ ...p, [k]: e.target.value })} /></Field>)}
        <Field label="Triangular start currencies"><input value={starts} onChange={(e) => setStarts(e.target.value)} /></Field>
        <Field label="Quote equivalents" hint="e.g. USD=USDC;USD=USDT (FX cost applied)"><input value={equiv} onChange={(e) => setEquiv(e.target.value)} /></Field>
      </div>
      <ErrorBox error={msg} />
      <Button variant="primary" onClick={save}>Save scanner parameters</Button>
    </Card>
  )
}

const RISK_FIELDS: [string, string][] = [
  ['max_position_gbp', 'Max position (£)'], ['max_total_exposure_gbp', 'Max total exposure (£)'], ['max_exchange_exposure_gbp', 'Max per-exchange exposure (£)'],
  ['max_asset_exposure_gbp', 'Max per-asset exposure (£)'], ['min_net_return_pct', 'Min net return (%)'], ['max_slippage_pct', 'Max depth slippage (%)'],
  ['max_data_age_ms', 'Max data age (ms)'], ['max_daily_loss_gbp', 'Max daily loss (£)'], ['max_open_order_exposure_gbp', 'Max open order exposure (£)'],
  ['max_inventory_imbalance_pct', 'Max inventory imbalance (%)'], ['circuit_breaker_failures', 'Circuit breaker: consecutive failures'],
  ['circuit_breaker_cooldown_s', 'Circuit breaker cooldown (s)'], ['duplicate_window_ms', 'Duplicate window (ms)'], ['max_trades_per_minute', 'Max trades / minute'],
]

function RiskSettings({ data }: { data: any }) {
  const qc = useQueryClient()
  const risk = useApi<any>('/risk', 5000)
  const events = useApi<any[]>('/risk/events?limit=50', 10000)
  const [live, setLive] = useState<any>(data.live_risk_limits)
  const [pw, setPw] = useState('')
  const [paper, setPaper] = useState<any>(null)
  const [err, setErr] = useState<unknown>(null)
  useEffect(() => { if (risk.data?.paper && !paper) setPaper(risk.data.paper.limits) }, [risk.data, paper])
  const saveLive = async () => { try { await put('/settings/live-risk', { limits: live, password: pw }); setPw(''); setErr(null); qc.invalidateQueries() } catch (e) { setErr(e) } }
  const savePaper = async () => { try { await put('/settings/paper-risk', { limits: paper }); setErr(null); qc.invalidateQueries() } catch (e) { setErr(e) } }
  const releaseKill = async () => { try { await post('/risk/kill-switch', { active: false, password: pw }); setPw(''); setErr(null); qc.invalidateQueries() } catch (e) { setErr(e) } }
  return (
    <div className="space-y-4">
      <Notice tone="accent">Risk limits are enforced by the deterministic risk engine on every order flow, independently of the AI agents. Defaults are deliberately restrictive.</Notice>
      {risk.data?.kill_switch && (
        <Card title="Kill switch active"><div className="flex items-end gap-2"><Field label="Password to release"><input type="password" value={pw} onChange={(e) => setPw(e.target.value)} /></Field><Button variant="danger" onClick={releaseKill}>Release kill switch</Button></div></Card>
      )}
      {paper && (
        <Card title="Current paper session limits (simulation)" bodyClass="space-y-3">
          <div className="grid gap-3 md:grid-cols-4">{RISK_FIELDS.map(([k, l]) => <Field key={k} label={l}><input value={paper[k] ?? ''} onChange={(e) => setPaper({ ...paper, [k]: e.target.value })} /></Field>)}</div>
          <Button onClick={savePaper}>Apply to paper session</Button>
        </Card>
      )}
      <Card title="Live trading limits (require password)" bodyClass="space-y-3">
        <div className="grid gap-3 md:grid-cols-4">{RISK_FIELDS.map(([k, l]) => <Field key={k} label={l}><input value={live[k] ?? ''} onChange={(e) => setLive({ ...live, [k]: e.target.value })} /></Field>)}</div>
        <div className="flex items-end gap-2"><Field label="Your password"><input type="password" value={pw} onChange={(e) => setPw(e.target.value)} /></Field><Button variant="primary" onClick={saveLive}>Save live limits</Button></div>
      </Card>
      <ErrorBox error={err} />
      <Card title="Risk events" bodyClass="p-0 max-h-72 overflow-auto">
        {!events.data?.length ? <Empty>No risk events.</Empty> : (
          <table className="data"><thead><tr><th>Time</th><th>Mode</th><th>Severity</th><th>Event</th></tr></thead>
            <tbody>{events.data.map((e, i) => <tr key={i}><td className="text-[11px]">{dt(e.ts)}</td><td>{e.mode}</td><td><Badge tone={e.severity === 'critical' ? 'neg' : 'warn'}>{e.severity}</Badge></td><td>{e.message}</td></tr>)}</tbody></table>
        )}
      </Card>
    </div>
  )
}

function AiSettings({ data }: { data: any }) {
  const s = data.settings
  return (
    <Card title="AI provider configuration">
      <KV rows={[
        ['Provider', s.ai_provider], ['Configured', s.ai_provider_configured ? 'yes' : 'no (deterministic analysis only)'],
        ['Anthropic model', s.anthropic_model], ['OpenAI-compatible model', s.openai_model], ['OpenAI-compatible base URL', s.openai_base_url ?? '—'],
        ['Monthly budget (USD)', s.ai_monthly_budget_usd], ['Scheduled agent runs', s.agents_schedule_enabled ? 'enabled' : 'disabled'],
        ['Scheduled runs use LLM', s.agents_schedule_use_llm ? 'yes' : 'no'], ['AI execution authority', 'Disabled (agents cannot trade)'],
      ]} />
      <p className="mt-3 text-[11px] text-muted">Set ARBIES_AI_PROVIDER=anthropic with ARBIES_ANTHROPIC_API_KEY, or ARBIES_AI_PROVIDER=openai with ARBIES_OPENAI_API_KEY / ARBIES_OPENAI_BASE_URL, in the backend environment. Keys are never sent to the browser.</p>
    </Card>
  )
}

function DataSettings({ data }: { data: any }) {
  const qc = useQueryClient()
  const [days, setDays] = useState(String(data.retention_days))
  const s = data.settings
  return (
    <Card title="Market data recording & retention" bodyClass="space-y-3">
      <KV rows={[['Market data mode', s.market_data_mode], ['Recording', s.record_market_data ? 'enabled' : 'disabled'], ['Snapshot interval', `${s.record_interval_ms} ms per market`], ['Order book depth', s.orderbook_depth]]} />
      <div className="flex items-end gap-2"><Field label="Retention (days)"><input value={days} onChange={(e) => setDays(e.target.value)} /></Field><Button onClick={async () => { await put('/settings/retention', { days: Number(days) }); qc.invalidateQueries() }}>Save</Button></div>
    </Card>
  )
}

function ModeSettings({ data }: { data: any }) {
  const qc = useQueryClient()
  const [preview, setPreview] = useState<any>(null)
  const [phrase, setPhrase] = useState('')
  const [pw, setPw] = useState('')
  const [err, setErr] = useState<unknown>(null)
  const gate = data.live_gate
  const [exchanges, setExchanges] = useState(data.settings.exchanges.join(','))
  const mkPreview = async () => { setErr(null); try { setPreview(await post('/live/preview', { exchanges: exchanges.split(',').map((x: string) => x.trim()).filter(Boolean) })) } catch (e) { setErr(e) } }
  const arm = async () => { setErr(null); try { await post('/live/arm', { preview_id: preview.preview_id, confirmation_phrase: phrase, password: pw }); setPreview(null); qc.invalidateQueries() } catch (e) { setErr(e) } }
  return (
    <div className="space-y-4">
      <Card title="Operating mode">
        <KV rows={[['Current mode', gate.armed ? 'LIVE (armed)' : 'Paper (simulation)'], ['Live trading enabled in backend env', gate.env_enabled ? 'yes' : 'no'], ['Armed exchanges', gate.exchanges.join(', ') || '—'], ['Arm expires', dt(gate.expires_at)]]} />
        <p className="mt-3 text-[12px] text-muted">Paper trading is the default. Live trading requires ARBIES_LIVE_TRADING_ENABLED=true on the backend, trade-permission credentials, your password, and the exact confirmation phrase after reviewing a preview. This screen alone cannot enable it, and automated strategy execution with real money is not activated in this release.</p>
        <div className="mt-3 flex items-end gap-2">
          <Field label="Exchanges"><input value={exchanges} onChange={(e) => setExchanges(e.target.value)} /></Field>
          <Button variant="danger" onClick={mkPreview}><ShieldAlert size={14} /> Review live trading preview</Button>
          {gate.armed && <Button onClick={async () => { await post('/live/disarm'); qc.invalidateQueries() }}>Disarm</Button>}
        </div>
        <div className="mt-2"><ErrorBox error={err} /></div>
      </Card>
      <Modal open={!!preview} onClose={() => setPreview(null)} title="Live trading preview — REAL MONEY" wide>
        {preview && (
          <div className="space-y-3 text-[12px]">
            {!preview.env_enabled && <Notice tone="neg">Live trading is disabled in the backend environment. Arming will be refused.</Notice>}
            <KV rows={[['Exchanges', preview.exchanges.join(', ')], ...Object.entries(preview.credential_permissions).map(([k, v]) => [`${k} credentials`, String(v)] as [string, string])]} />
            <div><div className="mb-1 text-[11px] uppercase text-muted">Position & risk limits</div><pre className="max-h-40 overflow-auto rounded bg-panel2 p-2 text-[11px]">{JSON.stringify(preview.risk_limits, null, 1)}</pre></div>
            <div><div className="mb-1 text-[11px] uppercase text-muted">Risks</div>{preview.risks.map((r: string, i: number) => <div key={i} className="text-warn">• {r}</div>)}</div>
            <Field label={`Type exactly: ${preview.confirmation_phrase}`}><input value={phrase} onChange={(e) => setPhrase(e.target.value)} /></Field>
            <Field label="Your password"><input type="password" value={pw} onChange={(e) => setPw(e.target.value)} /></Field>
            <ErrorBox error={err} />
            <div className="flex justify-end gap-2"><Button onClick={() => setPreview(null)}>Cancel</Button><Button variant="danger" onClick={arm}>Arm live trading</Button></div>
          </div>
        )}
      </Modal>
    </div>
  )
}

function Security({ data }: { data: any }) {
  const audit = useApi<any[]>('/system/audit?limit=200', 10000)
  return (
    <div className="space-y-4">
      <Card title="Security posture">
        <KV rows={[['Credential encryption key', data.settings.credential_encryption_configured ? 'configured' : 'NOT configured'], ['Environment', data.settings.env], ['Withdrawals', 'Never permitted (no code path)'], ['Agent permissions', 'read / simulate / propose only']]} />
      </Card>
      <Card title="Audit log" bodyClass="p-0 max-h-[60vh] overflow-auto">
        <table className="data"><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Detail</th><th>IP</th></tr></thead>
          <tbody>{(audit.data ?? []).map((a, i) => <tr key={i}><td className="text-[11px]">{dt(a.ts)}</td><td>{a.actor}</td><td>{a.action}</td><td>{a.target}</td><td className="max-w-[300px] truncate text-[11px]" title={JSON.stringify(a.detail)}>{JSON.stringify(a.detail)}</td><td className="text-[11px]">{a.ip}</td></tr>)}</tbody></table>
      </Card>
    </div>
  )
}
