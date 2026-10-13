import { useEffect, useState } from 'react'

const KEYS = ['accent', 'pos', 'neg', 'warn', 'sim', 'muted', 'line', 'text', 'panel'] as const
export type Palette = Record<(typeof KEYS)[number], string>

function read(): Palette {
  const s = getComputedStyle(document.documentElement)
  return Object.fromEntries(KEYS.map((k) => [k, s.getPropertyValue(`--${k === 'text' ? 'text' : k}`).trim()])) as Palette
}

/** Resolved theme colours for chart libraries that cannot use CSS variables. */
export function usePalette(): Palette {
  const [p, setP] = useState<Palette>(read)
  useEffect(() => {
    const obs = new MutationObserver(() => setP(read()))
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])
  return p
}

export const SERIES = ['#22d3ee', '#a78bfa', '#f59e0b', '#22c55e', '#f43f5e', '#60a5fa', '#e879f9', '#94a3b8']
