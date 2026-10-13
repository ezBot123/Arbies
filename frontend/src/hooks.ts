import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { api, type Tick } from './api'

export function useApi<T = any>(path: string | null, refetchMs?: number) {
  return useQuery<T>({
    queryKey: [path],
    queryFn: () => api<T>(path as string),
    enabled: !!path,
    refetchInterval: refetchMs,
  })
}

/** Live stream over WebSocket (authenticated by the session cookie). */
export function useStream(): { tick: Tick | null; connected: boolean } {
  const [tick, setTick] = useState<Tick | null>(null)
  const [connected, setConnected] = useState(false)
  const retry = useRef(0)
  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    let timer: number | undefined
    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      ws = new WebSocket(`${proto}://${location.host}/ws`)
      ws.onopen = () => { setConnected(true); retry.current = 0 }
      ws.onmessage = (e) => { try { setTick(JSON.parse(e.data)) } catch { /* ignore */ } }
      ws.onclose = () => {
        setConnected(false)
        if (!closed) timer = window.setTimeout(connect, Math.min(10000, 1000 * 2 ** retry.current++))
      }
    }
    connect()
    return () => { closed = true; clearTimeout(timer); ws?.close() }
  }, [])
  return { tick, connected }
}
