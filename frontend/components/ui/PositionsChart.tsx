'use client'

import type { Position } from '@/store/botStore'

interface PositionsChartProps {
  positions: Position[]
}

function formatCurrency(value: number) {
  const abs = Math.abs(value)
  return `$${abs.toFixed(2)}`
}

export function PositionsChart({ positions }: PositionsChartProps) {
  const data = positions
    .map((position) => {
      const pnl = Number.parseFloat(position.unrealized_pnl ?? '0')
      return {
        symbol: position.symbol,
        pnl: Number.isFinite(pnl) ? pnl : 0,
      }
    })
    .filter((item) => Number.isFinite(item.pnl))
    .sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl))
    .slice(0, 8)

  if (data.length === 0) {
    return <div className="rounded-lg border border-dashed border-slate-700 p-4 text-sm text-slate-500">Sem posições abertas para visualizar.</div>
  }

  const maxAbsPnl = Math.max(...data.map((item) => Math.abs(item.pnl)), 1)

  return (
    <div className="space-y-3">
      {data.map((item) => {
        const width = Math.max(10, (Math.abs(item.pnl) / maxAbsPnl) * 100)
        return (
          <div key={item.symbol} className="space-y-1">
            <div className="flex items-center justify-between text-xs text-slate-400">
              <span className="font-medium text-slate-300">{item.symbol}</span>
              <span className={item.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                {formatCurrency(item.pnl)}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-slate-800">
              <div
                className={`h-full rounded-full ${item.pnl >= 0 ? 'bg-emerald-500' : 'bg-red-500'}`}
                style={{ width: `${width}%` }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}
