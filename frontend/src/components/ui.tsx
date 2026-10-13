import clsx from 'clsx'
import { AlertTriangle, HelpCircle, Loader2, X } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { GLOSSARY } from '../lib/glossary'

export function Card({ title, actions, children, className, bodyClass, tip }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClass?: string; tip?: string
}) {
  return (
    <section className={clsx('rounded-lg border border-line bg-panel', className)}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
          <h2 className="flex items-center gap-1.5 text-[12px] font-semibold uppercase tracking-wide text-muted">
            {title}
            {tip && <Tip k={tip} />}
          </h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className={clsx('p-3', bodyClass)}>{children}</div>
    </section>
  )
}

export function Button({ children, onClick, variant = 'default', size = 'md', disabled, type = 'button', title, className }: {
  children: ReactNode; onClick?: () => void; variant?: 'default' | 'primary' | 'danger' | 'ghost' | 'success'
  size?: 'sm' | 'md'; disabled?: boolean; type?: 'button' | 'submit'; title?: string; className?: string
}) {
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        'inline-flex items-center justify-center gap-1.5 rounded-md border font-medium transition disabled:cursor-not-allowed disabled:opacity-50',
        size === 'sm' ? 'px-2 py-1 text-[12px]' : 'px-3 py-1.5 text-[13px]',
        variant === 'default' && 'border-line bg-panel2 hover:border-accent/60',
        variant === 'primary' && 'border-accent2 bg-accent2 text-white hover:bg-accent',
        variant === 'success' && 'border-pos/60 bg-pos/15 text-pos hover:bg-pos/25',
        variant === 'danger' && 'border-neg/60 bg-neg/15 text-neg hover:bg-neg/25',
        variant === 'ghost' && 'border-transparent hover:bg-panel2',
        className,
      )}
    >
      {children}
    </button>
  )
}

export function Badge({ children, tone = 'muted', title }: {
  children: ReactNode; tone?: 'muted' | 'pos' | 'neg' | 'warn' | 'sim' | 'accent' | 'live'; title?: string
}) {
  const tones: Record<string, string> = {
    muted: 'border-line text-muted', pos: 'border-pos/40 bg-pos/10 text-pos', neg: 'border-neg/40 bg-neg/10 text-neg',
    warn: 'border-warn/40 bg-warn/10 text-warn', sim: 'border-sim/40 bg-sim/10 text-sim',
    accent: 'border-accent/40 bg-accent/10 text-accent', live: 'border-live bg-live text-white',
  }
  return (
    <span title={title} className={clsx('inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium', tones[tone])}>
      {children}
    </span>
  )
}

export function Tip({ k, text }: { k?: string; text?: string }) {
  const t = text ?? (k ? GLOSSARY[k] : '')
  if (!t) return null
  return (
    <span className="group relative inline-flex cursor-help normal-case tracking-normal">
      <HelpCircle size={12} className="text-muted/70" />
      <span className="pointer-events-none absolute left-1/2 top-full z-50 mt-1 hidden w-64 -translate-x-1/2 rounded-md border border-line bg-panel2 p-2 text-[12px] font-normal text-fg shadow-xl group-hover:block">
        {t}
      </span>
    </span>
  )
}

export function Stat({ label, value, sub, tone, tip }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string; tip?: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel px-3 py-2.5">
      <div className="flex items-center gap-1 text-[11px] uppercase tracking-wide text-muted">
        {label}
        {tip && <Tip k={tip} />}
      </div>
      <div className={clsx('num mt-1 text-[19px] font-semibold', tone)}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-muted">{sub}</div>}
    </div>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-4 text-muted">
      <Loader2 size={14} className="animate-spin" /> {label ?? 'Loading…'}
    </div>
  )
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <div className="flex items-start gap-2 rounded-md border border-neg/40 bg-neg/10 p-2 text-[12px] text-neg">
      <AlertTriangle size={14} className="mt-0.5 shrink-0" /> <span className="whitespace-pre-wrap">{msg}</span>
    </div>
  )
}

export function Notice({ children, tone = 'warn' }: { children: ReactNode; tone?: 'warn' | 'sim' | 'accent' | 'neg' }) {
  const tones = { warn: 'border-warn/40 bg-warn/10 text-warn', sim: 'border-sim/40 bg-sim/10 text-sim', accent: 'border-accent/40 bg-accent/10 text-accent', neg: 'border-neg/40 bg-neg/10 text-neg' }
  return <div className={clsx('rounded-md border p-2 text-[12px]', tones[tone])}>{children}</div>
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="p-6 text-center text-[12px] text-muted">{children}</div>
}

export function Field({ label, children, tip, hint, group }: { label: string; children: ReactNode; tip?: string; hint?: string; group?: boolean }) {
  const inner = (
    <>
      <span className="flex items-center gap-1 text-muted">{label}{tip && <Tip text={tip} />}</span>
      {children}
      {hint && <span className="text-[11px] text-muted/80">{hint}</span>}
    </>
  )
  // groups of checkboxes must not be wrapped in a single <label>
  return group
    ? <div role="group" aria-label={label} className="flex flex-col gap-1 text-[12px]">{inner}</div>
    : <label className="flex flex-col gap-1 text-[12px]">{inner}</label>
}

export function Tabs({ tabs, value, onChange }: { tabs: { id: string; label: string }[]; value: string; onChange: (v: string) => void }) {
  return (
    <div className="flex gap-1 border-b border-line">
      {tabs.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={clsx('-mb-px border-b-2 px-3 py-1.5 text-[12px] font-medium',
            value === t.id ? 'border-accent text-fg' : 'border-transparent text-muted hover:text-fg')}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className={clsx('max-h-[90vh] w-full overflow-auto rounded-lg border border-line bg-panel shadow-2xl', wide ? 'max-w-4xl' : 'max-w-lg')}
           onClick={(e) => e.stopPropagation()}>
        <header className="flex items-center justify-between border-b border-line px-4 py-2.5">
          <h3 className="font-semibold">{title}</h3>
          <button onClick={onClose} className="text-muted hover:text-fg"><X size={16} /></button>
        </header>
        <div className="p-4">{children}</div>
      </div>
    </div>
  )
}

export function Confirm({ open, title, message, confirmLabel = 'Confirm', danger, onConfirm, onClose }: {
  open: boolean; title: string; message: ReactNode; confirmLabel?: string; danger?: boolean; onConfirm: () => void; onClose: () => void
}) {
  return (
    <Modal open={open} onClose={onClose} title={title}>
      <div className="space-y-4">
        <div className="text-[13px] text-muted">{message}</div>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant={danger ? 'danger' : 'primary'} onClick={() => { onConfirm(); onClose() }}>{confirmLabel}</Button>
        </div>
      </div>
    </Modal>
  )
}

export function useConfirm() {
  const [state, setState] = useState<null | { title: string; message: ReactNode; action: () => void; danger?: boolean; label?: string }>(null)
  const dialog = (
    <Confirm open={!!state} title={state?.title ?? ''} message={state?.message} danger={state?.danger}
             confirmLabel={state?.label} onConfirm={() => state?.action()} onClose={() => setState(null)} />
  )
  return { ask: (s: NonNullable<typeof state>) => setState(s), dialog }
}

export function KV({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px]">
      {rows.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="num text-right">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

export function SourceBadge({ source }: { source: string | null | undefined }) {
  if (source === 'synthetic') return <Badge tone="warn" title="Generated test data — not real market observations">SYNTHETIC DATA</Badge>
  if (source === 'live_recorded') return <Badge tone="accent" title="Real exchange market data">LIVE DATA</Badge>
  if (source === 'third_party_imported') return <Badge tone="muted">IMPORTED DATA</Badge>
  if (source === 'mixed') return <Badge tone="warn">MIXED SOURCES</Badge>
  return <Badge>NO DATA</Badge>
}
