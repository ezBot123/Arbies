import { Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { SERIES, usePalette } from '../lib/colors'

export function EquityChart({ series, height = 260 }: { series: { name: string; points: { ts: string; value: number }[] }[]; height?: number }) {
  const p = usePalette()
  // merge on timestamp index (each series plotted against its own elapsed time when comparing)
  const multi = series.length > 1
  const data: any[] = []
  if (!multi) {
    series[0]?.points.forEach((pt) => data.push({ x: new Date(pt.ts).getTime(), [series[0].name]: pt.value }))
  } else {
    series.forEach((s) => {
      const t0 = s.points.length ? new Date(s.points[0].ts).getTime() : 0
      s.points.forEach((pt) => data.push({ x: (new Date(pt.ts).getTime() - t0) / 60000, [s.name]: pt.value }))
    })
    data.sort((a, b) => a.x - b.x)
  }
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
        <CartesianGrid stroke={p.line} strokeDasharray="3 3" />
        <XAxis dataKey="x" type="number" domain={['dataMin', 'dataMax']} stroke={p.muted} fontSize={11}
               tickFormatter={(v) => (multi ? `${Math.round(v)}m` : new Date(v).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }))} />
        <YAxis stroke={p.muted} fontSize={11} domain={['auto', 'auto']} tickFormatter={(v) => `£${Number(v).toFixed(2)}`} width={70} />
        <Tooltip contentStyle={{ background: p.panel, border: `1px solid ${p.line}`, fontSize: 12 }}
                 labelFormatter={(v) => (multi ? `+${Number(v).toFixed(1)} min` : new Date(v).toLocaleString('en-GB'))}
                 formatter={(v: any) => `£${Number(v).toFixed(4)}`} />
        {multi && <Legend wrapperStyle={{ fontSize: 11 }} />}
        {series.map((s, i) => (
          <Line key={s.name} type="stepAfter" dataKey={s.name} stroke={SERIES[i % SERIES.length]} dot={false} strokeWidth={1.6} connectNulls isAnimationActive={false} />
        ))}
      </LineChart>
    </ResponsiveContainer>
  )
}

export function PnlBars({ data, height = 220 }: { data: { name: string; value: number }[]; height?: number }) {
  const p = usePalette()
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
        <CartesianGrid stroke={p.line} strokeDasharray="3 3" />
        <XAxis dataKey="name" stroke={p.muted} fontSize={10} interval={0} angle={data.length > 4 ? -15 : 0} height={data.length > 4 ? 40 : 20} />
        <YAxis stroke={p.muted} fontSize={11} tickFormatter={(v) => `£${Number(v).toFixed(2)}`} width={60} />
        <Tooltip contentStyle={{ background: p.panel, border: `1px solid ${p.line}`, fontSize: 12 }} formatter={(v: any) => `£${Number(v).toFixed(4)}`} />
        <Bar dataKey="value" isAnimationActive={false}>
          {data.map((d, i) => <Cell key={i} fill={d.value >= 0 ? p.pos : p.neg} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

/** Several series on a shared calendar axis (merged by timestamp; gaps are not interpolated). */
export function DateSeriesChart({ series, height = 280, money = true }: {
  series: { name: string; points: { ts: string; value: number }[]; dashed?: boolean; color?: string }[]
  height?: number
  money?: boolean
}) {
  const p = usePalette()
  const byX = new Map<number, any>()
  series.forEach((s) => s.points.forEach((pt) => {
    const x = new Date(pt.ts).getTime()
    const row = byX.get(x) ?? { x }
    row[s.name] = pt.value
    byX.set(x, row)
  }))
  const data = [...byX.values()].sort((a, b) => a.x - b.x)
  const fmt = (v: number) => (money ? `£${Number(v).toLocaleString('en-GB', { maximumFractionDigits: 0 })}` : String(v))
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
        <CartesianGrid stroke={p.line} strokeDasharray="3 3" />
        <XAxis dataKey="x" type="number" scale="time" domain={['dataMin', 'dataMax']} stroke={p.muted} fontSize={11}
               tickFormatter={(v) => new Date(v).toLocaleDateString('en-GB', { month: 'short', year: '2-digit' })} />
        <YAxis stroke={p.muted} fontSize={11} domain={['auto', 'auto']} tickFormatter={fmt} width={70} />
        <Tooltip contentStyle={{ background: p.panel, border: `1px solid ${p.line}`, fontSize: 12 }}
                 labelFormatter={(v) => new Date(v).toLocaleDateString('en-GB')}
                 formatter={(v: any) => (money ? `£${Number(v).toFixed(2)}` : v)} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        {series.map((s, i) => (
          <Line key={s.name} type="stepAfter" dataKey={s.name} stroke={s.color ?? SERIES[i % SERIES.length]} dot={false}
                strokeWidth={1.6} strokeDasharray={s.dashed ? '4 3' : undefined} connectNulls isAnimationActive={false} />
        ))}
      </LineChart>
    </ResponsiveContainer>
  )
}
