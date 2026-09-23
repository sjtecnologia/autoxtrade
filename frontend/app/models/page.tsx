'use client'

import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useModels, useTrainJobStatus, useTriggerOptimizeTimeframes, useTriggerTrain } from '@/hooks/useModels'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { MetricCard } from '@/components/ui/MetricCard'
import { MARKETS } from '@/lib/constants'
import { api, type MLModel } from '@/lib/api'

const MARKET_COLORS: Record<string, string> = {
  CRIPTO: 'text-yellow-400',
  FOREX: 'text-blue-400',
  B3: 'text-emerald-400',
}

const MT5_TRAIN_TIMEFRAMES = [
  '1m', '3m', '5m', '15m', '30m',
  '1h', '2h', '4h', '6h', '8h', '12h',
  '1d', '1w',
]

function pct(v: number | null, decimals = 1) {
  if (v == null) return '—'
  return `${(v * 100).toFixed(decimals)}%`
}

function num(v: number | null, decimals = 2) {
  if (v == null) return '—'
  return v.toFixed(decimals)
}

function ModelRow({ model }: { model: MLModel }) {
  const statusColor =
    model.status === 'active'
      ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
      : model.status === 'retired'
        ? 'bg-slate-700 text-slate-400'
        : 'bg-slate-700/50 text-slate-500'

  return (
    <tr className="border-b border-slate-800 hover:bg-slate-800/40 transition-colors">
      <td className="px-4 py-3 font-mono text-xs text-slate-400 max-w-[200px] truncate">
        {model.version}
      </td>
      <td className="px-4 py-3">
        <span className={`text-sm font-medium ${MARKET_COLORS[model.market] ?? 'text-white'}`}>
          {model.market}
        </span>
      </td>
      <td className="px-4 py-3 text-sm text-white">{model.symbol ?? '—'}</td>
      <td className="px-4 py-3">
        <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${statusColor}`}>
          {model.status}
        </span>
      </td>
      <td className="px-4 py-3 font-mono text-sm text-right">
        {model.ml_accuracy != null ? (
          <span className={model.ml_accuracy >= 0.55 ? 'text-emerald-400' : 'text-slate-400'}>
            {pct(model.ml_accuracy)}
          </span>
        ) : (
          <span className="text-slate-500">—</span>
        )}
      </td>
      <td className="px-4 py-3 font-mono text-sm text-right">
        {model.bt_win_rate != null ? (
          <span className={model.bt_win_rate >= 0.5 ? 'text-emerald-400' : 'text-red-400'}>
            {pct(model.bt_win_rate)}
          </span>
        ) : (
          <span className="text-slate-500">—</span>
        )}
      </td>
      <td className="px-4 py-3 font-mono text-sm text-right">
        {model.bt_profit_factor != null ? (
          <span className={model.bt_profit_factor >= 1.2 ? 'text-emerald-400' : 'text-red-400'}>
            {num(model.bt_profit_factor)}
          </span>
        ) : (
          <span className="text-slate-500">—</span>
        )}
      </td>
      <td className="px-4 py-3 font-mono text-sm text-right text-slate-400">
        {num(model.bt_sharpe_ratio)}
      </td>
      <td className="px-4 py-3 font-mono text-sm text-right text-slate-400">
        {model.bt_total_trades ?? '—'}
      </td>
      <td className="px-4 py-3 font-mono text-sm text-right text-slate-400">
        {model.train_samples.toLocaleString()}
      </td>
      <td className="px-4 py-3 text-xs text-slate-500 whitespace-nowrap">
        {new Date(model.trained_at).toLocaleString('pt-BR', {
          day: '2-digit', month: '2-digit', year: '2-digit',
          hour: '2-digit', minute: '2-digit',
        })}
      </td>
    </tr>
  )
}

export default function ModelsPage() {
  const [selectedMarket, setSelectedMarket] = useState<string | undefined>(undefined)
  const [trainMarket, setTrainMarket] = useState<string>('FOREX')
  const [trainTimeframe, setTrainTimeframe] = useState('1h')
  const [trainProfile, setTrainProfile] = useState<'didi' | 'robust'>('didi')
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  const [jobStatusFilter, setJobStatusFilter] = useState<'all' | 'failed' | 'rejected' | 'running' | 'completed'>('all')
  const [toast, setToast] = useState<string | null>(null)
  const [optMarket, setOptMarket] = useState<string>('FOREX')
  const [optSymbol, setOptSymbol] = useState('')
  const [optTfSelection, setOptTfSelection] = useState<string[]>(['15m', '30m', '1h', '4h'])
  const [optProfile, setOptProfile] = useState<'didi' | 'robust'>('didi')

  const { data: models = [], isLoading, refetch } = useModels(selectedMarket)
  const { data: trainJob } = useTrainJobStatus(activeJobId)
  const { mutate: triggerTrain, isPending: isTraining } = useTriggerTrain()
  const { mutate: triggerOptimizeTimeframes, isPending: isOptimizing } = useTriggerOptimizeTimeframes()
  const { mutate: removeSymbol, isPending: isRemovingSymbol } = useMutation({
    mutationFn: (payload: { market: string; symbol: string }) =>
      api.removeSymbolFromSystem({ ...payload, from_all_markets: false }),
  })

  const filteredSymbols = (trainJob?.symbols ?? []).filter((item) => {
    if (jobStatusFilter === 'all') return true
    return item.status === jobStatusFilter
  })

  function downloadJobJson() {
    if (!trainJob) return
    const blob = new Blob([JSON.stringify(trainJob, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `train-job-${trainJob.job_id}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  async function downloadJobCsv() {
    if (!trainJob) return
    try {
      const csvText = await api.downloadTrainReportCsv(trainJob.job_id)
      const blob = new Blob([csvText], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `train-report-${trainJob.job_id}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Falha ao baixar CSV'
      setToast(`Erro no CSV: ${message}`)
      setTimeout(() => setToast(null), 5000)
    }
  }

  const activeCount = models.filter((m) => m.status === 'active').length
  const totalApproved = models.filter((m) => m.is_approved).length

  function handleTrain() {
    triggerTrain(
      { market: trainMarket, timeframe: trainTimeframe, profile: trainProfile },
      {
        onSuccess: (res) => {
          setActiveJobId(res.job_id)
          setToast(res.message)
          setTimeout(() => setToast(null), 5000)
        },
        onError: (err) => {
          setToast(`Erro: ${err.message}`)
          setTimeout(() => setToast(null), 5000)
        },
      }
    )
  }

  function toggleOptimizeTf(tf: string) {
    setOptTfSelection((prev) => {
      if (prev.includes(tf)) {
        return prev.filter((x) => x !== tf)
      }
      return [...prev, tf]
    })
  }

  function handleOptimize() {
    if (optTfSelection.length === 0) {
      setToast('Selecione ao menos 1 timeframe para otimização.')
      setTimeout(() => setToast(null), 4000)
      return
    }

    triggerOptimizeTimeframes(
      {
        market: optMarket || undefined,
        symbol: optSymbol.trim() || undefined,
        timeframes: optTfSelection,
        profile: optProfile,
      },
      {
        onSuccess: (res) => {
          setActiveJobId(res.job_id)
          setToast(res.message)
          setTimeout(() => setToast(null), 6000)
        },
        onError: (err) => {
          setToast(`Erro na otimização: ${err.message}`)
          setTimeout(() => setToast(null), 6000)
        },
      }
    )
  }

  function handleRemoveSymbol(market: string, symbol: string) {
    const confirmRemove = window.confirm(`Remover ${symbol} do mercado ${market}?`)
    if (!confirmRemove) return
    removeSymbol(
      { market, symbol },
      {
        onSuccess: (res) => {
          if (res.removed_from.length > 0) {
            setToast(`Símbolo ${symbol} removido de: ${res.removed_from.join(', ')}`)
          } else {
            setToast(`Símbolo ${symbol} não estava ativo para remoção em ${market}.`)
          }
          setTimeout(() => setToast(null), 5000)
        },
        onError: (err) => {
          setToast(`Erro ao remover ${symbol}: ${err.message}`)
          setTimeout(() => setToast(null), 6000)
        },
      }
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white">Modelos ML</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Treinamento e métricas — sem negociações até aprovação
          </p>
        </div>
        <button
          onClick={() => refetch()}
          className="text-xs text-slate-400 hover:text-white border border-slate-700 rounded px-3 py-1.5 transition-colors"
        >
          Atualizar
        </button>
      </div>

      {/* Toast */}
      {toast && (
        <div className="bg-blue-500/20 border border-blue-500/40 rounded-lg px-4 py-3 text-sm text-blue-300">
          {toast}
        </div>
      )}

      {/* Resumo */}
      <div className="grid grid-cols-3 gap-4">
        <MetricCard label="Total de modelos" value={String(models.length)} />
        <MetricCard label="Ativos" value={String(activeCount)} />
        <MetricCard label="Aprovados" value={String(totalApproved)} />
      </div>

      {/* Painel de treinamento */}
      <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5">
        <h2 className="text-sm font-semibold text-white mb-4">Iniciar Treinamento</h2>
        <div className="flex flex-wrap gap-3 items-end">
          <div className="flex flex-col gap-1">
            <label className="text-xs text-slate-400">Mercado</label>
            <select
              value={trainMarket}
              onChange={(e) => setTrainMarket(e.target.value)}
              className="bg-slate-700 border border-slate-600 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
            >
              {MARKETS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
              <option value="">Todos os mercados</option>
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-xs text-slate-400">Timeframe</label>
            <select
              value={trainTimeframe}
              onChange={(e) => setTrainTimeframe(e.target.value)}
              className="bg-slate-700 border border-slate-600 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
            >
              {MT5_TRAIN_TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>{tf}</option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-xs text-slate-400">Perfil</label>
            <select
              value={trainProfile}
              onChange={(e) => setTrainProfile(e.target.value as 'didi' | 'robust')}
              className="bg-slate-700 border border-slate-600 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
            >
              <option value="didi">Didi (aulas/transcrições)</option>
              <option value="robust">Robust (gates estritos)</option>
            </select>
          </div>
          <button
            onClick={handleTrain}
            disabled={isTraining}
            className="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-wait rounded text-sm font-medium text-white transition-colors"
          >
            {isTraining ? 'Iniciando…' : 'Treinar agora'}
          </button>
          <p className="text-xs text-slate-500 self-end pb-1.5">
            O treinamento roda em background. Recarregue em alguns minutos.
          </p>
        </div>

        <div className="mt-5 pt-4 border-t border-slate-700">
          <h3 className="text-sm font-semibold text-white mb-3">Otimizar Timeframe por Ativo</h3>
          <div className="flex flex-wrap gap-3 items-end">
            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400">Mercado</label>
              <select
                value={optMarket}
                onChange={(e) => setOptMarket(e.target.value)}
                className="bg-slate-700 border border-slate-600 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
              >
                {MARKETS.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
                <option value="">Todos os mercados</option>
              </select>
            </div>

            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400">Símbolo (opcional)</label>
              <input
                value={optSymbol}
                onChange={(e) => setOptSymbol(e.target.value)}
                placeholder="Ex.: EURUSD.pr"
                className="bg-slate-700 border border-slate-600 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
              />
            </div>

            <div className="flex flex-col gap-1">
              <label className="text-xs text-slate-400">Perfil</label>
              <select
                value={optProfile}
                onChange={(e) => setOptProfile(e.target.value as 'didi' | 'robust')}
                className="bg-slate-700 border border-slate-600 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
              >
                <option value="didi">Didi (aulas/transcrições)</option>
                <option value="robust">Robust (gates estritos)</option>
              </select>
            </div>

            <button
              onClick={handleOptimize}
              disabled={isOptimizing}
              className="px-4 py-1.5 bg-violet-600 hover:bg-violet-500 disabled:opacity-50 disabled:cursor-wait rounded text-sm font-medium text-white transition-colors"
            >
              {isOptimizing ? 'Iniciando otimização…' : 'Otimizar timeframes'}
            </button>
          </div>

          <div className="mt-3 flex flex-wrap gap-2">
            {MT5_TRAIN_TIMEFRAMES.map((tf) => {
              const checked = optTfSelection.includes(tf)
              return (
                <label
                  key={tf}
                  className={`text-xs px-2 py-1 rounded border cursor-pointer transition-colors ${
                    checked
                      ? 'border-violet-500/50 bg-violet-500/15 text-violet-200'
                      : 'border-slate-700 bg-slate-900/40 text-slate-400 hover:text-slate-200'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleOptimizeTf(tf)}
                    className="hidden"
                  />
                  {tf}
                </label>
              )
            })}
          </div>

          <p className="text-[11px] text-slate-500 mt-2">
            A otimização testa os timeframes selecionados para cada ativo no perfil escolhido e tenta salvar apenas o melhor.
          </p>
        </div>
      </div>

      {/* Progresso de treinamento */}
      {trainJob && (
        <div className="bg-slate-800/60 border border-slate-700 rounded-xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-white">Progresso do treinamento</h3>
              <p className="text-xs text-slate-400 mt-0.5">
                Job {trainJob.job_id.slice(0, 8)} · {trainJob.timeframe} · {trainJob.status}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <select
                value={jobStatusFilter}
                onChange={(e) => setJobStatusFilter(e.target.value as 'all' | 'failed' | 'rejected' | 'running' | 'completed')}
                className="bg-slate-700 border border-slate-600 rounded px-2 py-1 text-xs text-white"
              >
                <option value="all">Todos</option>
                <option value="running">Em execução</option>
                <option value="completed">Aprovados</option>
                <option value="rejected">Rejeitados</option>
                <option value="failed">Falhas</option>
              </select>
              <button
                onClick={downloadJobJson}
                className="text-xs text-slate-300 hover:text-white border border-slate-600 rounded px-2 py-1"
              >
                Baixar JSON
              </button>
              <button
                onClick={downloadJobCsv}
                className="text-xs text-slate-300 hover:text-white border border-slate-600 rounded px-2 py-1"
              >
                Baixar CSV
              </button>
              <div className="text-xs text-slate-300">
                {trainJob.completed_symbols + trainJob.failed_symbols + trainJob.rejected_symbols}/{trainJob.total_symbols} símbolos processados
              </div>
            </div>
          </div>

          <div className="h-2 w-full rounded bg-slate-700 overflow-hidden">
            <div
              className="h-full bg-blue-500 transition-all duration-500"
              style={{ width: `${trainJob.progress_pct}%` }}
            />
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <div className="rounded border border-slate-700 bg-slate-900/40 px-3 py-2 text-slate-300">
              Aprovados: <span className="text-emerald-400 font-medium">{trainJob.approved_symbols}</span>
            </div>
            <div className="rounded border border-slate-700 bg-slate-900/40 px-3 py-2 text-slate-300">
              Rejeitados: <span className="text-amber-400 font-medium">{trainJob.rejected_symbols}</span>
            </div>
            <div className="rounded border border-slate-700 bg-slate-900/40 px-3 py-2 text-slate-300">
              Falhas: <span className="text-red-400 font-medium">{trainJob.failed_symbols}</span>
            </div>
            <div className="rounded border border-slate-700 bg-slate-900/40 px-3 py-2 text-slate-300">
              Progresso: <span className="text-blue-400 font-medium">{trainJob.progress_pct}%</span>
            </div>
          </div>

          <div className="max-h-64 overflow-auto rounded border border-slate-700">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-slate-900/40 border-b border-slate-700">
                  <th className="px-3 py-2 text-left text-slate-400 font-medium">Mercado</th>
                  <th className="px-3 py-2 text-left text-slate-400 font-medium">Símbolo</th>
                  <th className="px-3 py-2 text-left text-slate-400 font-medium">Status</th>
                  <th className="px-3 py-2 text-left text-slate-400 font-medium">Detalhe</th>
                  <th className="px-3 py-2 text-left text-slate-400 font-medium">Ação</th>
                </tr>
              </thead>
              <tbody>
                {filteredSymbols.map((item) => (
                  <tr key={`${item.market}-${item.symbol}`} className="border-b border-slate-800">
                    <td className="px-3 py-2 text-slate-300">{item.market}</td>
                    <td className="px-3 py-2 text-white font-mono">{item.symbol}</td>
                    <td className="px-3 py-2">
                      <span
                        className={`px-2 py-0.5 rounded-full border ${
                          item.status === 'completed'
                            ? 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10'
                            : item.status === 'rejected'
                              ? 'text-amber-300 border-amber-500/40 bg-amber-500/10'
                              : item.status === 'failed'
                                ? 'text-red-400 border-red-500/40 bg-red-500/10'
                                : item.status === 'running'
                                  ? 'text-blue-400 border-blue-500/40 bg-blue-500/10'
                                  : 'text-slate-400 border-slate-600 bg-slate-700/30'
                        }`}
                      >
                        {item.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-400 truncate max-w-[420px]">{item.message}</td>
                    <td className="px-3 py-2">
                      {(item.status === 'failed' || item.status === 'rejected') ? (
                        <button
                          onClick={() => handleRemoveSymbol(item.market, item.symbol)}
                          disabled={isRemovingSymbol}
                          className="text-[11px] px-2 py-1 rounded border border-red-700 text-red-300 hover:bg-red-900/30 disabled:opacity-40"
                        >
                          Remover do sistema
                        </button>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                  </tr>
                ))}
                {filteredSymbols.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-3 py-4 text-center text-slate-500">
                      Nenhum símbolo para o filtro selecionado.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Filtro por mercado */}
      <div className="flex gap-2">
        <button
          onClick={() => setSelectedMarket(undefined)}
          className={`text-xs px-3 py-1 rounded-full border transition-colors ${
            selectedMarket == null
              ? 'bg-white/10 border-white/30 text-white'
              : 'border-slate-700 text-slate-400 hover:text-white'
          }`}
        >
          Todos
        </button>
        {MARKETS.map((m) => (
          <button
            key={m}
            onClick={() => setSelectedMarket(m)}
            className={`text-xs px-3 py-1 rounded-full border transition-colors ${
              selectedMarket === m
                ? 'bg-white/10 border-white/30 text-white'
                : 'border-slate-700 text-slate-400 hover:text-white'
            }`}
          >
            {m}
          </button>
        ))}
      </div>

      {/* Tabela */}
      <div className="bg-slate-800/60 border border-slate-700 rounded-xl overflow-hidden">
        {isLoading ? (
          <div className="p-8 text-center text-slate-500 text-sm">Carregando modelos…</div>
        ) : models.length === 0 ? (
          <div className="p-8 text-center">
            <p className="text-slate-400 text-sm">Nenhum modelo treinado ainda.</p>
            <p className="text-slate-500 text-xs mt-1">
              Clique em "Treinar agora" para iniciar o primeiro treinamento.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700 bg-slate-900/40">
                  {[
                    'Versão', 'Mercado', 'Símbolo', 'Status',
                    'Accuracy', 'Win Rate', 'Profit Factor', 'Sharpe',
                    'Trades', 'Amostras', 'Treinado em',
                  ].map((h) => (
                    <th
                      key={h}
                      className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider whitespace-nowrap"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {models.map((model) => (
                  <ModelRow key={model.id} model={model} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
