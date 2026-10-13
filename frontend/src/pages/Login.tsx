import { BarChart3 } from 'lucide-react'
import { useState } from 'react'
import { post } from '../api'
import { Button, ErrorBox } from '../components/ui'

export default function Login({ onLogin }: { onLogin: (u: string) => void }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const r = await post<{ username: string }>('/auth/login', { username, password })
      onLogin(r.username)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex h-full items-center justify-center">
      <form onSubmit={submit} className="w-80 space-y-3 rounded-lg border border-line bg-panel p-6">
        <div className="mb-2 flex items-center gap-2">
          <BarChart3 className="text-accent" />
          <span className="text-lg font-bold">Arbies</span>
        </div>
        <p className="text-[12px] text-muted">Arbitrage research &amp; paper-trading platform. Sign in to continue.</p>
        <input className="w-full" placeholder="Username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        <input className="w-full" placeholder="Password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        <ErrorBox error={error} />
        <Button type="submit" variant="primary" disabled={busy} className="w-full">{busy ? 'Signing in…' : 'Sign in'}</Button>
        <p className="text-[11px] text-muted">The admin password is set with ARBIES_ADMIN_PASSWORD, or generated and printed in the backend log on first start.</p>
      </form>
    </div>
  )
}
