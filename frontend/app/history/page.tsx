'use client'

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type TradeSummary } from '@/lib/api'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { MetricCard } from '@/components/ui/MetricCard'
import { PnlValue } from '@/components/ui/PnlValue'
import { EquityCurve } from '@/components/charts/EquityCurve'
import { useEquityHistory } from '@/hooks/useEquityHistory'
import { exportToCsv } from '@/lib/export'
import { formatDate, formatPercent, formatDuration } from '@/lib/formatters'
import { MARKETS } from '@/lib/constants'

const PER_PAGE = 50

export default function HistoryPage() {
  const [market, setMarket] = useState('')
  const [result, setResult] = useState('')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [page, setPage] = useState(1)

  const { data: history, isLoading } = useQuery({
    queryKey: ['trade-history', page, market, result, startDate, endDate],
    queryFn: () =>
      api.getTradeHistory({
        page,
        per_page: PER_PAGE,
        market: market || undefined,
        result: result || undefined,
        start_date: startDate || undefined,
        end_date: endDate || undefined,
      }),
    staleTime: 30_000,
  })

  const { data: perf } = useQuery({
    queryKey: ['performance', market, startDate, endDate],
    queryFn: () =>
      api.getPerformance({
        market: market || undefined,
        start_date: startDate || undefined,
        end_date: endDate || undefined,
      }),
    staleTime: 30_000,
  })

  const { data: equity } = useEquityHistory(market || 'CRIPTO', 720)

  const columns: Column<TradeSummary>[] = [
    { key: 'symbol', header: 'Par', className: 'font-medium text-white' },
    { key: 'market', header: 'Mercado' },
    {
      key: 'side',
      header: 'Lado',
      render: (r) => (
        <span className={r.side === 'LONG' ? 'text-emerald-400' : 'text-red-400'}>{r.side}</span>
      ),
    },
    {
      key: 'entry_price',
      header: 'Entrada',
      render: (r) => <span className="font-mono">${parseFloat(r.entry_price).toFixed(2)}</span>,
    },
    {
      key: 'exit_price',
      header: 'Saida',
      render: (r) =>
        r.exit_price ? (
          <span className="font-mono">${parseFloat(r.exit_price).toFixed(2)}</span>
        ) : (
          <span className="text-slate-500">-</span>
        ),
    },
    {
      key: 'pnl_net',
      header: 'PnL Net',
      render: (r) => <PnlValue value={r.pnl_net} />,
    },
    {
      key: 'pnl_pct',
      header: '%',
      render: (r) => <PnlValue value={r.pnl_pct} showSign />,
    },
    {
      key: 'duration_sec',
      header: 'Duracao',
      render: (r) => formatDuration(r.duration_sec),
    },
    { key: 'open_at', header: 'Abertura', render: (r) => formatDate(r.open_at) },
    {
      key: 'close_reason',
      header: 'Motivo',
      render: (r) => <span className="text-xs text-slate-500">{r.close_reason ?? '-'}</span>,
    },
  ]

  function handleExport() {
    if (!history?.trades) return
    exportToCsv(
      history.trades.map((t) => ({
        par: t.symbol,
        mercado: t.market,
        lado: t.side,
        modo: t.mode,
        entrada: t.entry_price,
        saida: t.exit_price ?? '',
        quantidade: t.quantity,
        pnl_bruto: t.pnl_gross ?? '',
        pnl_net: t.pnl_net ?? '',
        pnl_pct: t.pnl_pct ?? '',
        abertura: t.open_at,
        fechamento: t.close_at ?? '',
        duracao_seg: t.duration_sec ?? '',
        motivo: t.close_reason ?? '',
        sinal_ml: t.ml_signal ?? '',
        confianca_ml: t.ml_confidence ?? '',
      })),
      `trades_${new Date().toISOString().split('T')[0]}.csv`,
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white">Historico de Trades</h1>
          <p className="text-sm text-slate-400">Resultados e metricas de performance</p>
        </div>
        <button
          onClick={handleExport}
          disabled={!history?.trades?.length}
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40 transition-colors"
        >
          Exportar CSV
        </button>
      </div>

      {/* Filtros */}
      <div className="flex flex-wrap gap-3 rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <select
          value={market}
          onChange={(e) => { setMarket(e.target.value); setPage(1) }}
          className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-300"
        >
          <option value="">Todos os mercados</option>
          {MARKETS.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>

        <select
          value={result}
          onChange={(e) => { setResult(e.target.value); setPage(1) }}
          className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-300"
        >
          <option value="">Todos os resultados</option>
          <option value="WIN">WIN</option>
          <option value="LOSS">LOSS</option>
        </select>

        <input
          type="date"
          value={startDate}
          onChange={(e) => { setStartDate(e.target.value); setPage(1) }}
          className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-300"
        />
        <input
          type="date"
          value={endDate}
          onChange={(e) => { setEndDate(e.target.value); setPage(1) }}
          className="rounded-lg border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-300"
        />
        {(market || result || startDate || endDate) && (
          <button
            onClick={() => { setMarket(''); setResult(''); setStartDate(''); setEndDate(''); setPage(1) }}
            className="text-xs text-slate-400 hover:text-white"
          >
            Limpar filtros
          </button>
        )}
      </div>

      {/* Performance metrics */}
      {perf && (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <MetricCard label="P&L Total" value={`$${parseFloat(perf.total_pnl).toFixed(2)}`} />
          <MetricCard label="Trades" value={String(perf.trades_count)} />
          <MetricCard label="Win Rate" value={formatPercent(perf.win_rate, 1)} />
          <MetricCard label="Profit Factor" value={parseFloat(perf.profit_factor).toFixed(2)} />
          <MetricCard label="PnL Medio" value={`$${parseFloat(perf.avg_pnl).toFixed(2)}`} />
          <MetricCard label="Duracao Med." value={formatDuration(perf.avg_duration_sec)} />
        </div>
      )}

      {/* Equity Curve */}
      <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-300 uppercase tracking-wide">
          Curva de Equity
        </h2>
        <EquityCurve data={equity ?? []} height={280} />
      </div>

      {/* Tabela */}
      <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300 uppercase tracking-wide">
            Trades ({history?.total ?? 0})
          </h2>
          {history && history.pages > 1 && (
            <div className="flex items-center gap-2 text-sm text-slate-400">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="rounded px-2 py-1 hover:bg-slate-800 disabled:opacity-40"
              >
                &lsaquo;
              </button>
              <span>{page} / {history.pages}</span>
              <button
                onClick={() => setPage((p) => Math.min(history.pages, p + 1))}
                disabled={page >= history.pages}
                className="rounded px-2 py-1 hover:bg-slate-800 disabled:opacity-40"
              >
                &rsaquo;
              </button>
            </div>
          )}
        </div>
        <DataTable
          columns={columns}
          data={history?.trades ?? []}
          isLoading={isLoading}
          emptyMessage="Nenhum trade encontrado"
        />
      </div>
    </div>
  )
}
