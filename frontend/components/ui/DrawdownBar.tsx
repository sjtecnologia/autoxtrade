'use client'

import clsx from 'clsx'

interface DrawdownBarProps {
  value: string | number
  thresholdWarning?: number
  thresholdDanger?: number
  label?: string
}

export function DrawdownBar({
  value,
  thresholdWarning = 10,
  thresholdDanger = 15,
  label,
}: DrawdownBarProps) {
  const pct = parseFloat(String(value))
  const capped = Math.min(pct, 100)

  const isWarning = pct >= thresholdWarning && pct < thresholdDanger
  const isDanger = pct >= thresholdDanger

  return (
    <div className="space-y-1">
      {label && (
        <div className="flex justify-between text-xs text-slate-400">
          <span>{label}</span>
          <span
            className={clsx(
              'font-medium',
              isDanger && 'text-red-400',
              isWarning && 'text-yellow-400',
              !isDanger && !isWarning && 'text-emerald-400',
            )}
          >
            {pct.toFixed(2)}%
          </span>
        </div>
      )}
      <div className="h-2 w-full rounded-full bg-slate-700 overflow-hidden">
        <div
          className={clsx(
            'h-full rounded-full transition-all duration-500',
            isDanger && 'bg-red-500',
            isWarning && 'bg-yellow-400',
            !isDanger && !isWarning && 'bg-emerald-500',
          )}
          style={{ width: `${capped}%` }}
        />
      </div>
    </div>
  )
}
