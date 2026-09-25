'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { useQuery } from '@tanstack/react-query'
import { usePositions } from '@/hooks/usePositions'
import { useDrawdown } from '@/hooks/useDrawdown'
import { useDayMetrics } from '@/hooks/useDayMetrics'
import { useEquityHistory } from '@/hooks/useEquityHistory'
import { MetricCard } from '@/components/ui/MetricCard'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { DrawdownBar } from '@/components/ui/DrawdownBar'
import { PnlValue } from '@/components/ui/PnlValue'
import { Sparkline } from '@/components/ui/Sparkline'
import { ToastAlert } from '@/components/ui/ToastAlert'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { LiveCandles } from '@/components/ui/LiveCandles'
import { formatPercent, formatDate } from '@/lib/formatters'
import { api } from '@/lib/api'
import { API_URL } from '@/lib/constants'
import type { Position } from '@/store/botStore'
import { useBotStore } from '@/store/botStore'
import { MARKETS } from '@/lib/constants'

export default function DashboardPage() {
  const { positions, wsConnected, error, isLoading } = usePositions()
  const { data: drawdown } = useDrawdown('CRIPTO')
  const { data: dayMetrics } = useDayMetrics()
  const { data: equityHistory } = useEquityHistory('CRIPTO', 24)
  const {
    data: pendingApprovals,
    isLoading: pendingApprovalsLoading,
    error: pendingApprovalsError,
  } = useQuery({
    queryKey: ['approvals', 'pending', 'dashboard'],
    queryFn: () => api.getEntryApprovals('pending'),
    refetchInterval: 10_000,
  })
  const botStatus = useBotStore((s) => s.botStatus)

  const [showAlert, setShowAlert] = useState(false)

  useEffect(() => {
    if (drawdown?.status === 'PAUSED' || parseFloat(drawdown?.dd_30d ?? '0') >= 10) {
      setShowAlert(true)
    }
  }, [drawdown?.status, drawdown?.dd_30d])

  const ddValue = drawdown ? parseFloat(drawdown.dd_30d) : 0
  const isWarning = ddValue >= 10 && ddValue < 15
  const isDanger = ddValue >= 15

  const getDisplaySide = (value?: string) => {
    const side = value?.toUpperCase()
    if (side === 'LONG' || side === 'BUY') return 'LONG'
    if (side === 'SHORT' || side === 'SELL') return 'SHORT'
    return value ?? '—'
  }

  const formatPriceValue = (value: string | number | null | undefined) => {
    const parsed = value == null || value === '' ? null : Number.parseFloat(String(value))
    if (parsed == null || Number.isNaN(parsed)) {
      return <span className="font-mono text-slate-500">—</span>
    }
    return <span className="font-mono">${parsed.toFixed(2)}</span>
  }

  const positionColumns: Column<Position>[] = [
    { key: 'symbol', header: 'Par', className: 'font-medium text-white' },
    {
      key: 'side',
      header: 'Lado',
      render: (row) => {
        const side = getDisplaySide(row.side)
        return <span className={side === 'LONG' ? 'text-emerald-400' : 'text-red-400'}>{side}</span>
      },
    },
    {
      key: 'entry_price',
      header: 'Entrada',
      render: (row) => formatPriceValue(row.entry_price),
    },
    {
      key: 'current_price',
      header: 'Atual',
      render: (row) => formatPriceValue(row.current_price),
    },
    { key: 'quantity', header: 'Qty', render: (row) => <span className="font-mono">{row.quantity}</span> },
    {
      key: 'unrealized_pnl',
      header: 'PnL U.',
      render: (row) => <PnlValue value={row.unrealized_pnl} />,
    },
    {
      key: 'unrealized_pnl_pct',
      header: '%',
      render: (row) => <PnlValue value={row.unrealized_pnl_pct} showSign />,
    },
    { key: 'open_at', header: 'Abertura', render: (row) => formatDate(row.open_at) },
  ]

  return (
    <div className="space-y-6">
      {(isWarning || isDanger) && showAlert && (
        <ToastAlert
          variant={isDanger ? 'danger' : 'warning'}
          message={
            isDanger
              ? `Bot PAUSADO - drawdown ${ddValue.toFixed(1)}% atingiu o limite`
              : `Atencao - drawdown ${ddValue.toFixed(1)}% proximo do limite (15%)`
          }
          onDismiss={() => setShowAlert(false)}
        />
      )}

      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold text-white">Dashboard</h1>
            <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-[0.2em] text-emerald-300">
              LIVE
            </span>
          </div>
          <p className="text-sm text-slate-400">Posicoes em tempo real</p>
        </div>
        <div className="flex items-center gap-3">
          <span className={`flex items-center gap-1 text-xs ${wsConnected ? 'text-emerald-400' : 'text-slate-500'}`}>
            <span className={`h-2 w-2 rounded-full ${wsConnected ? 'bg-emerald-400 animate-pulse' : 'bg-slate-600'}`} />
            {wsConnected ? 'WS conectado' : 'WS desconectado'}
          </span>
          {MARKETS.map((m) => (
            <div key={m} className="flex items-center gap-1.5">
              <span className="text-xs text-slate-400">{m}</span>
              <StatusBadge status={(botStatus[m] as 'ATIVO' | 'PAUSADO') ?? 'DESCONECTADO'} />
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2 rounded-xl border border-slate-800 bg-slate-900/60 p-4">
          <div className="mb-2 flex justify-between text-xs text-slate-400">
            <span>Equity 24h</span>
            {equityHistory && equityHistory.length > 0 && (
              <span className="font-mono">
                ${parseFloat(equityHistory[equityHistory.length - 1].equity).toFixed(2)}
              </span>
            )}
          </div>
          <Sparkline data={equityHistory ?? []} height={80} />
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4 space-y-3">
          <p className="text-xs font-medium text-slate-400 uppercase tracking-wide">Drawdown</p>
          <DrawdownBar value={drawdown?.dd_30d ?? '0'} label="30 dias" />
          <DrawdownBar value={drawdown?.dd_1d ?? '0'} label="1 dia" />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <MetricCard
          label="P&L Hoje"
          value={
            dayMetrics
              ? `${parseFloat(dayMetrics.day_pnl) >= 0 ? '+' : ''}$${parseFloat(dayMetrics.day_pnl).toFixed(2)}`
              : '...'
          }
        />
        <MetricCard label="Trades Hoje" value={dayMetrics ? String(dayMetrics.trades_count) : '...'} />
        <MetricCard
          label="Win Rate"
          value={dayMetrics ? formatPercent(dayMetrics.win_rate, 1) : '...'}
        />
      </div>

      <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
            Aprovações DiDi Pendentes ({pendingApprovals?.length ?? 0})
          </h2>
          <Link
            href="/approvals"
            className="rounded-md border border-blue-700 px-2.5 py-1 text-xs font-medium text-blue-300 hover:bg-blue-900/30"
          >
            Abrir central de aprovações
          </Link>
        </div>
        <p className="mb-2 text-xs text-slate-500">Fonte API: {API_URL}</p>

        {pendingApprovalsError ? (
          <p className="text-sm text-red-400">
            Falha ao carregar aprovações pendentes.
          </p>
        ) : pendingApprovalsLoading ? (
          <p className="text-sm text-slate-400">Carregando aprovações pendentes...</p>
        ) : (!pendingApprovals || pendingApprovals.length === 0) ? (
          <p className="text-sm text-slate-400">Sem entradas aguardando decisão no momento.</p>
        ) : (
          <div className="space-y-2">
            {pendingApprovals.slice(0, 5).map((ap) => (
              <div
                key={ap.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2"
              >
                <div className="text-sm text-slate-200">
                  <span className="font-semibold">{ap.market}</span> · <span className="font-semibold">{ap.symbol}</span> ·{' '}
                  <span className={ap.side === 'buy' ? 'text-emerald-400' : 'text-red-400'}>
                    {ap.side === 'buy' ? 'COMPRA' : 'VENDA'}
                  </span>
                </div>
                <div className="text-xs text-slate-400">
                  Criada em {formatDate(ap.created_at)}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
            Posicoes Abertas ({positions.length})
          </h2>
          <div className="flex items-center gap-2 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1 text-[11px] font-medium text-emerald-300">
            <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
            {isLoading ? 'Atualizando a cada 2s' : 'Atualização em tempo real'}
          </div>
        </div>
        {error ? (
          <div className="mb-3 rounded-md border border-red-900/40 bg-red-950/30 p-3 text-sm text-red-300">
            Falha ao carregar posicoes: {error instanceof Error ? error.message : String(error)}
          </div>
        ) : null}
        <div className="mb-4 rounded-lg border border-slate-800 bg-slate-950/40 p-3">
          <LiveCandles positions={positions} />
        </div>
        <DataTable
          columns={positionColumns}
          data={positions}
          keyField="id"
          emptyMessage="Nenhuma posicao aberta"
        />
      </div>
    </div>
  )
}
