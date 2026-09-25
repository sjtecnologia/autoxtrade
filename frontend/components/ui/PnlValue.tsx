'use client'

import clsx from 'clsx'

interface PnlValueProps {
  value: string | number | null | undefined
  showSign?: boolean
  className?: string
}

export function PnlValue({ value, showSign = true, className }: PnlValueProps) {
  const num = value == null ? null : parseFloat(String(value))
  if (num == null || isNaN(num)) return <span className="text-slate-500">—</span>

  const isPositive = num > 0
  const isNegative = num < 0

  return (
    <span
      className={clsx(
        'font-mono font-medium tabular-nums',
        isPositive && 'text-emerald-400',
        isNegative && 'text-red-400',
        !isPositive && !isNegative && 'text-slate-400',
        className,
      )}
    >
      {showSign && isPositive ? '+' : ''}
      {num.toFixed(2)}
    </span>
  )
}
