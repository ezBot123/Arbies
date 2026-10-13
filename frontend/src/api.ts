// Thin API client. The session cookie is httpOnly; credentials never touch frontend storage.

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let onUnauthorized: () => void = () => {}
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: opts.method ?? 'GET',
    credentials: 'include',
    headers: opts.body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  })
  if (res.status === 401 && !path.startsWith('/auth/login')) {
    onUnauthorized()
  }
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail ?? j)
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, msg)
  }
  const ct = res.headers.get('content-type') ?? ''
  return (ct.includes('application/json') ? res.json() : res.text()) as Promise<T>
}

export const post = <T = any>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body: body ?? {} })
export const put = <T = any>(path: string, body?: unknown) => api<T>(path, { method: 'PUT', body })
export const del = <T = any>(path: string) => api<T>(path, { method: 'DELETE' })

// ---- shared types (subset of backend payloads) ----
export interface Opportunity {
  id: string
  key: string
  kind: 'cross_exchange' | 'triangular'
  exchanges: string[]
  symbols: string[]
  route: string[]
  buy_exchange: string | null
  sell_exchange: string | null
  age_ms: number
  profit_currency: string
  size_base: string
  notional: string
  notional_gbp: string | null
  gross_spread_pct: string
  gross_profit: string
  fees: string
  depth_slippage: string
  slippage_buffer: string
  latency_cost: string
  rebalancing_cost: string
  fx_cost: string
  leg_failure_cost: string
  total_costs: string
  net_profit: string
  net_profit_gbp: string | null
  net_return_pct: string
  max_size_base: string
  data_age_ms: number
  confidence: number
  qualified: boolean
  rejection_reasons: string[]
  rejection_explanations: string[]
  legs: { exchange: string; symbol: string; side: string; base_amount: string; vwap: string; quote_amount: string; fee: string; fee_currency: string; top_price: string; levels_used: number }[]
  data_source: string
}

export interface Tick {
  type: 'tick'
  ts: string
  data_source: string
  exchanges: Record<string, { status: string; healthy: boolean; transport: string; updates: number; errors: number }>
  tops: { exchange: string; symbol: string; bid: string | null; ask: string | null; age_ms: number; source: string }[]
  opportunities: Opportunity[]
  scanner: Record<string, number>
  paper: null | { status: string; value_gbp: string; total_pnl_gbp: string; daily_realised_pnl_gbp: string; session_id: number; counters: Record<string, number> }
  kill_switch: boolean
  live_armed: boolean
}
