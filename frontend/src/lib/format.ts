export function toNum(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

export function gbp(v: string | number | null | undefined, digits = 2): string {
  const n = toNum(v)
  if (n === null) return '—'
  const abs = Math.abs(n)
  const d = abs !== 0 && abs < 0.01 ? 4 : digits
  return (n < 0 ? '-£' : '£') + abs.toLocaleString('en-GB', { minimumFractionDigits: d, maximumFractionDigits: d })
}

const FIAT = new Set(['USD', 'EUR', 'USDC', 'USDT', 'GBP'])

export function money(v: string | number | null | undefined, ccy: string, digits?: number): string {
  const n = toNum(v)
  if (n === null) return '—'
  if (ccy === 'GBP') return gbp(n)
  const d = digits ?? (FIAT.has(ccy) ? 4 : 8)
  return `${n.toLocaleString('en-GB', { maximumFractionDigits: d, minimumFractionDigits: Math.min(2, d) })} ${ccy}`
}

export function pct(v: string | number | null | undefined, digits = 3): string {
  const n = toNum(v)
  if (n === null) return '—'
  return `${n >= 0 ? '' : ''}${n.toFixed(digits)}%`
}

export function num(v: string | number | null | undefined, digits = 6): string {
  const n = toNum(v)
  if (n === null) return '—'
  return n.toLocaleString('en-GB', { maximumFractionDigits: digits })
}

export function price(v: string | number | null | undefined): string {
  const n = toNum(v)
  if (n === null) return '—'
  const d = n >= 1000 ? 2 : n >= 1 ? 4 : 8
  return n.toLocaleString('en-GB', { minimumFractionDigits: Math.min(d, 2), maximumFractionDigits: d })
}

export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—'
  if (v < 1000) return `${Math.round(v)} ms`
  if (v < 60000) return `${(v / 1000).toFixed(1)} s`
  return `${Math.round(v / 60000)} min`
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = Date.now() - new Date(iso).getTime()
  if (d < 60_000) return `${Math.max(0, Math.round(d / 1000))}s ago`
  if (d < 3_600_000) return `${Math.round(d / 60_000)}m ago`
  if (d < 86_400_000) return `${Math.round(d / 3_600_000)}h ago`
  return new Date(iso).toLocaleDateString('en-GB')
}

export function dt(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-GB', { dateStyle: 'short', timeStyle: 'medium' })
}

export function signClass(v: string | number | null | undefined): string {
  const n = toNum(v)
  if (n === null || n === 0) return ''
  return n > 0 ? 'text-pos' : 'text-neg'
}

export const label = (s: string) => s.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
