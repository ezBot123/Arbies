import { useQueryClient } from '@tanstack/react-query'
import { Bot, Play } from 'lucide-react'
import { useState } from 'react'
import { post } from '../api'
import { PageTitle } from '../components/Layout'
import { Badge, Button, Card, Empty, ErrorBox, Modal, Notice, Stat } from '../components/ui'
import { useApi } from '../hooks'
import { ago, dt } from '../lib/format'

export default function Agents() {
  const qc = useQueryClient()
  const agents = useApi<any[]>('/agents', 5000)
  const tasks = useApi<any[]>('/agents/tasks?limit=60', 5000)
  const recs = useApi<any[]>('/agents/recommendations', 5000)
  const usage = useApi<any>('/agents/usage', 15000)
  const settings = useApi<any>('/settings')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [task, setTask] = useState<any>(null)
  const aiConfigured = !!settings.data?.settings?.ai_provider_configured
  const [llmPref, setUseLlm] = useState(true)
  const useLlm = aiConfigured && llmPref

  const run = async (name: string) => {
    setBusy(name); setError(null)
    try {
      const r = await post(`/agents/${name}/run`, { input: {}, use_llm: useLlm })
      qc.invalidateQueries()
      const t = (await qc.fetchQuery({ queryKey: ['/agents/tasks?limit=60'] }) as any[]) ?? []
      setTask(t.find((x) => x.id === r.task_id) ?? null)
    } catch (e) { setError(e) } finally { setBusy(null) }
  }
  const decide = async (id: number, decision: string) => { await post(`/agents/recommendations/${id}`, { decision }); qc.invalidateQueries({ queryKey: ['/agents/recommendations'] }) }
  const lastTask = (name: string) => tasks.data?.find((t) => t.agent === name)

  return (
    <div className="space-y-4">
      <PageTitle title="AI Agent Centre" subtitle="Agents analyse real system data through permissioned, logged tools. They can research, simulate and propose — they cannot trade, enable live mode, change risk limits or move funds." />
      {!aiConfigured && <Notice tone="accent">No AI provider is configured, so agents return their deterministic analysis only. Set ARBIES_AI_PROVIDER and an API key on the backend to add LLM-written reports.</Notice>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Agents" value={agents.data?.length ?? '—'} />
        <Stat label="Open recommendations" value={recs.data?.filter((r) => r.status === 'open').length ?? '—'} />
        <Stat label="AI spend this month" value={`$${Number(usage.data?.month_spend_usd ?? 0).toFixed(4)}`} sub={`budget $${usage.data?.monthly_budget_usd ?? '—'}`} />
        <Stat label="Tasks recorded" value={tasks.data?.length ?? 0} />
      </div>
      <ErrorBox error={error} />
      <div className="flex items-center gap-2 text-[12px]"><label className="flex items-center gap-1.5"><input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} disabled={!aiConfigured} /> Use LLM narrative (costs tokens)</label></div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {(agents.data ?? []).map((a) => {
          const lt = lastTask(a.name)
          return (
            <Card key={a.name} title={<span className="flex items-center gap-1.5 normal-case"><Bot size={13} /> {a.title}</span>}
                  actions={<Button size="sm" variant="primary" disabled={!!busy || a.running} onClick={() => run(a.name)}><Play size={12} />{busy === a.name || a.running ? 'Running…' : 'Run'}</Button>}>
              <p className="text-[12px] text-muted">{a.description}</p>
              <div className="mt-2 flex flex-wrap gap-1">{a.tools.map((t: string) => <Badge key={t}>{t}</Badge>)}</div>
              <div className="mt-2 flex items-center justify-between text-[11px] text-muted">
                <span>{a.schedule_minutes ? `default schedule every ${a.schedule_minutes} min` : 'on demand'}</span>
                {lt && <button className="text-accent" onClick={() => setTask(lt)}>last: {lt.status} {ago(lt.created_at)}</button>}
              </div>
              {lt?.output?.summary && <div className="mt-2 rounded border border-line p-2 text-[12px]">{lt.output.summary}</div>}
            </Card>
          )
        })}
      </div>
      <Card title="Recommendations (require human decision)" bodyClass="p-0 max-h-96 overflow-auto">
        {!recs.data?.length ? <Empty>No recommendations yet.</Empty> : (
          <table className="data">
            <thead><tr><th>When</th><th>Agent</th><th>Severity</th><th>Recommendation</th><th>Status</th><th></th></tr></thead>
            <tbody>{recs.data.map((r) => (
              <tr key={r.id}>
                <td className="text-[11px]">{ago(r.created_at)}</td><td>{r.agent}</td>
                <td><Badge tone={r.severity === 'critical' ? 'neg' : r.severity === 'warning' ? 'warn' : 'muted'}>{r.severity}</Badge></td>
                <td className="max-w-[520px] whitespace-normal"><div className="font-medium">{r.title}</div><div className="text-[12px] text-muted">{r.detail}</div>{r.proposed_change && <code className="text-[11px] text-accent">{JSON.stringify(r.proposed_change)}</code>}</td>
                <td><Badge tone={r.status === 'approved' ? 'pos' : r.status === 'open' ? 'accent' : 'muted'}>{r.status}</Badge></td>
                <td>{r.status === 'open' && <div className="flex gap-1"><Button size="sm" variant="success" onClick={() => decide(r.id, 'approved')}>Approve</Button><Button size="sm" onClick={() => decide(r.id, 'rejected')}>Reject</Button></div>}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Card>
      <p className="text-[11px] text-muted">Approving a recommendation records your decision; it never changes live trading. Apply simulation changes yourself in Strategy Manager or Settings.</p>
      <Card title="Agent activity history" bodyClass="p-0 max-h-96 overflow-auto">
        <table className="data">
          <thead><tr><th>#</th><th>Agent</th><th>Trigger</th><th>Status</th><th>Started</th><th className="r">Tool calls</th><th>LLM</th><th className="r">Tokens</th><th className="r">Est. cost</th></tr></thead>
          <tbody>{(tasks.data ?? []).map((t) => (
            <tr key={t.id} className="cursor-pointer" onClick={() => setTask(t)}>
              <td className="num">{t.id}</td><td>{t.agent}</td><td className="text-muted">{t.trigger}</td>
              <td><Badge tone={t.status === 'completed' ? 'pos' : t.status === 'failed' ? 'neg' : 'accent'}>{t.status}</Badge></td>
              <td className="text-[11px]">{dt(t.created_at)}</td><td className="r num">{t.tool_calls.length}</td>
              <td>{t.llm_used ? t.model : '—'}</td><td className="r num">{t.input_tokens + t.output_tokens}</td><td className="r num">${Number(t.est_cost_usd).toFixed(4)}</td>
            </tr>
          ))}</tbody>
        </table>
      </Card>
      <Modal open={!!task} onClose={() => setTask(null)} title={task ? `${task.agent} · task #${task.id}` : ''} wide>
        {task && (
          <div className="space-y-3 text-[12px]">
            {task.error && <ErrorBox error={task.error} />}
            {task.output?.summary && <div className="text-[14px] font-medium">{task.output.summary}</div>}
            {task.output?.narrative && <div className="whitespace-pre-wrap rounded border border-line bg-panel2 p-3">{task.output.narrative}</div>}
            {task.output?.findings?.length > 0 && <div><div className="mb-1 text-[11px] uppercase text-muted">Findings</div>{task.output.findings.map((f: string, i: number) => <div key={i}>• {f}</div>)}</div>}
            {task.output?.recommendations?.length > 0 && <div><div className="mb-1 text-[11px] uppercase text-muted">Recommendations</div>{task.output.recommendations.map((r: any, i: number) => <div key={i}><b>{r.title}</b> — {r.detail}</div>)}</div>}
            <div><div className="mb-1 text-[11px] uppercase text-muted">Tool calls (audited)</div>
              {task.tool_calls.map((c: any, i: number) => <div key={i} className="num text-[11px]"><span className={c.ok ? 'text-pos' : 'text-neg'}>{c.ok ? '✓' : '✗'}</span> {c.tool}({JSON.stringify(c.args)}) → {c.summary.slice(0, 160)}</div>)}</div>
          </div>
        )}
      </Modal>
    </div>
  )
}
