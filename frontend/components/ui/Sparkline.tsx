'use client'

import { AreaChart, Area, ResponsiveContainer, Tooltip } from 'recharts'

interface SparklineProps {
  data: Array<{ timestamp: string; equity: string }>
  height?: number
}

export function Sparkline({ data, height = 48 }: SparklineProps) {
  if (!data || data.length < 2) {
    return <div className="h-12 flex items-center text-xs text-slate-500">sem dados</div>
  }

  const first = parseFloat(data[0].equity)
  const last = parseFloat(data[data.length - 1].equity)
  const color = last >= first ? '#10b981' : '#ef4444'

  const parsed = data.map((d) => ({ equity: parseFloat(d.equity) }))

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={parsed} margin={{ top: 2, right: 0, bottom: 2, left: 0 }}>
        <defs>
          <linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={color} stopOpacity={0.3} />
            <stop offset="95%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <Area
          type="monotone"
          dataKey="equity"
          stroke={color}
          strokeWidth={1.5}
          fill="url(#sparkGrad)"
          dot={false}
          isAnimationActive={false}
        />
        <Tooltip
          contentStyle={{ background: '#1e293b', border: 'none', borderRadius: 6, fontSize: 11 }}
          formatter={(v: number) => [`$${v.toFixed(2)}`, 'Equity']}
          labelFormatter={() => ''}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
