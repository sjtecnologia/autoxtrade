'use client'

import {
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  Brush,
  ResponsiveContainer,
} from 'recharts'
import type { EquityPoint } from '@/lib/api'

interface EquityCurveProps {
  data: EquityPoint[]
  height?: number
}

function formatXAxis(timestamp: string): string {
  return new Date(timestamp).toLocaleDateString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
  })
}

export function EquityCurve({ data, height = 300 }: EquityCurveProps) {
  if (!data || data.length === 0) {
    return (
      <div
        className="flex items-center justify-center rounded-lg bg-slate-800/40 text-sm text-slate-500"
        style={{ height }}
      >
        Sem dados de equity
      </div>
    )
  }

  const parsed = data.map((d) => ({
    timestamp: d.timestamp,
    equity: parseFloat(d.equity),
    drawdown: parseFloat(d.drawdown_pct),
  }))

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={parsed} margin={{ top: 10, right: 20, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
            <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis
          dataKey="timestamp"
          tickFormatter={formatXAxis}
          tick={{ fontSize: 11, fill: '#94a3b8' }}
          tickLine={false}
          axisLine={{ stroke: '#334155' }}
        />
        <YAxis
          yAxisId="equity"
          orientation="left"
          tick={{ fontSize: 11, fill: '#94a3b8' }}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => `$${v.toFixed(0)}`}
        />
        <YAxis
          yAxisId="dd"
          orientation="right"
          tick={{ fontSize: 11, fill: '#f87171' }}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => `${v.toFixed(1)}%`}
        />
        <Tooltip
          contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 12 }}
          labelFormatter={(v: string) => new Date(v).toLocaleString('pt-BR')}
          formatter={(value: number, name: string) => [
            name === 'equity' ? `$${value.toFixed(2)}` : `${value.toFixed(2)}%`,
            name === 'equity' ? 'Equity' : 'Drawdown',
          ]}
        />
        <Legend
          wrapperStyle={{ fontSize: 12, color: '#94a3b8' }}
          formatter={(v) => (v === 'equity' ? 'Equity' : 'Drawdown')}
        />
        <Area
          yAxisId="equity"
          type="monotone"
          dataKey="equity"
          stroke="#3b82f6"
          strokeWidth={2}
          fill="url(#equityGrad)"
          dot={false}
          isAnimationActive={false}
        />
        <Line
          yAxisId="dd"
          type="monotone"
          dataKey="drawdown"
          stroke="#f87171"
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />
        <Brush
          dataKey="timestamp"
          height={20}
          stroke="#334155"
          fill="#0f172a"
          tickFormatter={formatXAxis}
          travellerWidth={6}
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}
