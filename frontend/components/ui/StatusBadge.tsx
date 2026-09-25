import clsx from 'clsx'

type Status = 'ok' | 'error' | 'degraded' | 'active' | 'paused' | 'paper' | 'live' | string

const palette: Record<string, string> = {
  ok: 'bg-green-500/20 text-green-400 ring-green-500/30',
  active: 'bg-green-500/20 text-green-400 ring-green-500/30',
  ativo: 'bg-green-500/20 text-green-400 ring-green-500/30',
  live: 'bg-green-500/20 text-green-400 ring-green-500/30',
  error: 'bg-red-500/20 text-red-400 ring-red-500/30',
  paused: 'bg-yellow-500/20 text-yellow-400 ring-yellow-500/30',
  pausado: 'bg-yellow-500/20 text-yellow-400 ring-yellow-500/30',
  degraded: 'bg-yellow-500/20 text-yellow-400 ring-yellow-500/30',
  desconectado: 'bg-slate-500/20 text-slate-400 ring-slate-500/30',
  paper: 'bg-blue-500/20 text-blue-400 ring-blue-500/30',
}

export function StatusBadge({ status }: { status: Status }) {
  const cls = palette[status.toLowerCase()] ?? 'bg-slate-500/20 text-slate-400 ring-slate-500/30'
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset',
        cls
      )}
    >
      {status}
    </span>
  )
}
