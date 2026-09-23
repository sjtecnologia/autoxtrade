import clsx from 'clsx'
import type { ReactNode } from 'react'

interface MetricCardProps {
  label: string
  value: ReactNode
  sub?: string
  highlight?: boolean
}

export function MetricCard({ label, value, sub, highlight }: MetricCardProps) {
  return (
    <div
      className={clsx(
        'rounded-xl border p-4',
        highlight
          ? 'border-brand bg-brand/10'
          : 'border-slate-700 bg-slate-800'
      )}
    >
      <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</p>
      <div className="mt-1 text-2xl font-semibold text-white">{value}</div>
      {sub && <p className="mt-0.5 text-xs text-slate-500">{sub}</p>}
    </div>
  )
}
