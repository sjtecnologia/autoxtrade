'use client'

import { useMemo, useState } from 'react'
import dynamic from 'next/dynamic'
import { useQuery } from '@tanstack/react-query'
import { api, type EntryApproval } from '@/lib/api'
import { formatDate } from '@/lib/formatters'

const CandlestickChart = dynamic(() => import('@/components/CandlestickChart'), {
  ssr: false,
  loading: () => (
    <div className="flex h-[360px] w-full items-center justify-center rounded-lg border border-slate-700 bg-slate-800/50">
      <span className="animate-pulse text-sm text-slate-400">Carregando gráfico...</span>
    </div>
  ),
})

const OVERLAY_STYLE: Record<string, { label: string; color: string; pane: number }> = {
  ma3: { label: 'Didi MA3', color: '#f97316', pane: 0 },
  ma8: { label: 'Didi MA8', color: '#e2e8f0', pane: 0 },
  ma20: { label: 'Didi MA20', color: '#38bdf8', pane: 0 },
  bb_upper: { label: 'Bollinger sup.', color: '#a855f7', pane: 0 },
  bb_lower: { label: 'Bollinger inf.', color: '#a855f7', pane: 0 },
  adx: { label: 'ADX', color: '#facc15', pane: 1 },
  plus_di: { label: '+DI', color: '#22c55e', pane: 1 },
  minus_di: { label: '-DI', color: '#ef4444', pane: 1 },
}

const CRITERIA_LABEL: Record<string, string> = {
  didi_signal: 'Sinal Didi',
  dmi_trend: 'Tendência DMI',
  adx_accelerating: 'ADX acelerando',
  bollinger_open: 'Bollinger abrindo',
}

function num(value?: string | null): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function fmt(value?: string | null, digits = 4): string {
  const parsed = num(value)
  return parsed === null ? '—' : parsed.toFixed(digits)
}

function fmtMoney(value?: string | null): string {
  const parsed = num(value)
  if (parsed === null) return '—'
  return parsed.toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' })
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'up' | 'down' | 'warn' }) {
  const toneCls =
    tone === 'up' ? 'text-emerald-300' : tone === 'down' ? 'text-red-300' : tone === 'warn' ? 'text-yellow-300' : 'text-white'
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-950/50 px-3 py-2">
      <p className="text-[11px] uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`font-mono text-sm font-semibold ${toneCls}`}>{value}</p>
    </div>
  )
}

interface Props {
  approval: EntryApproval
  onApprove: (id: string, quantity: string) => void
  onReject: (id: string) => void
  isSubmitting: boolean
}

export default function EntryApprovalCard({ approval, onApprove, onReject, isSubmitting }: Props) {
  const [quantity, setQuantity] = useState(approval.quantity)
  const isPending = approval.status === 'pending'
  const isBuy = approval.side === 'buy'

  const { data: chart, isLoading: chartLoading } = useQuery({
    queryKey: ['approval-chart', approval.id],
    queryFn: () => api.getEntryApprovalChart(approval.id),
    staleTime: 5 * 60_000,
    retry: false,
  })

  const overlayLines = useMemo(() => {
    if (!chart?.overlays) return []
    return Object.entries(chart.overlays)
      .filter(([key, points]) => OVERLAY_STYLE[key] && points.length > 0)
      .map(([key, points]) => ({
        id: key,
        label: OVERLAY_STYLE[key].label,
        color: OVERLAY_STYLE[key].color,
        paneIndex: OVERLAY_STYLE[key].pane,
        data: points,
      }))
  }, [chart])

  const ratios = useMemo(() => {
    const entry = num(approval.entry_price)
    const suggested = num(approval.suggested_quantity ?? approval.quantity)
    const riskUnit = num(approval.risk_per_unit)
    const qty = Number(quantity)
    if (!entry || !riskUnit || !Number.isFinite(qty) || qty <= 0) return null
    const target = num(approval.take_profit)
    const gainSafe = num(approval.gain_safe)
    return {
      suggested,
      risk: riskUnit * qty,
      gainTarget: target ? Math.abs(target - entry) * qty : null,
      gainSafeValue: gainSafe ? Math.abs(gainSafe - entry) * qty : null,
    }
  }, [approval, quantity])

  const quantityChanged = quantity !== (approval.suggested_quantity ?? approval.quantity)

  return (
    <article className="space-y-4 rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-bold text-white">{approval.symbol}</h2>
            <span
              className={`rounded-full border px-2 py-0.5 text-xs font-semibold uppercase ${
                isBuy
                  ? 'border-emerald-700 bg-emerald-900/30 text-emerald-300'
                  : 'border-red-700 bg-red-900/30 text-red-300'
              }`}
            >
              {isBuy ? 'Compra' : 'Venda'}
            </span>
            <span className="rounded-full border border-slate-700 px-2 py-0.5 text-xs uppercase text-slate-300">
              {approval.status}
            </span>
          </div>
          <p className="text-xs text-slate-400">
            {approval.market} · {approval.timeframe || 'timeframe n/d'} · modo {approval.mode} · detectado em{' '}
            {formatDate(approval.created_at)} · expira em {formatDate(approval.expires_at)}
          </p>
        </div>

        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs text-slate-400">
            Quantidade (cotas)
            <input
              type="number"
              min={0}
              step="any"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              disabled={!isPending}
              className="mt-1 block w-32 rounded-md border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-sm text-white disabled:opacity-40"
            />
          </label>
          <button
            onClick={() => onApprove(approval.id, quantity)}
            disabled={!isPending || isSubmitting || !(Number(quantity) > 0)}
            className="rounded-md border border-emerald-700 bg-emerald-900/30 px-3 py-2 text-sm font-semibold text-emerald-300 hover:bg-emerald-900/60 disabled:opacity-40"
          >
            Autorizar entrada
          </button>
          <button
            onClick={() => onReject(approval.id)}
            disabled={!isPending || isSubmitting}
            className="rounded-md border border-red-700 bg-red-900/20 px-3 py-2 text-sm font-semibold text-red-300 hover:bg-red-900/50 disabled:opacity-40"
          >
            Rejeitar
          </button>
        </div>
      </header>

      {quantityChanged && isPending && (
        <p className="text-xs text-yellow-300">
          Quantidade ajustada manualmente (sugestão do robô: {approval.suggested_quantity ?? approval.quantity}).
        </p>
      )}

      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-6">
        <Stat label="Entrada" value={fmt(approval.entry_price)} />
        <Stat label="Loss (stop)" value={fmt(approval.stop_loss)} tone="down" />
        <Stat label="Gain (alvo)" value={fmt(approval.take_profit)} tone="up" />
        <Stat label="Gain safe (1R)" value={fmt(approval.gain_safe)} tone="up" />
        <Stat label="Loss safe (break-even)" value={fmt(approval.loss_safe)} tone="warn" />
        <Stat label="Risco/retorno" value={approval.risk_reward ? `${fmt(approval.risk_reward, 1)}R` : '—'} />
        <Stat label="Qtd sugerida" value={approval.suggested_quantity ?? approval.quantity} />
        <Stat label="Risco financeiro" value={ratios ? fmtMoney(String(ratios.risk)) : fmtMoney(approval.risk_amount)} tone="down" />
        <Stat
          label="Ganho no alvo"
          value={ratios?.gainTarget != null ? fmtMoney(String(ratios.gainTarget)) : fmtMoney(approval.potential_gain)}
          tone="up"
        />
        <Stat
          label="Ganho no gain safe"
          value={ratios?.gainSafeValue != null ? fmtMoney(String(ratios.gainSafeValue)) : '—'}
          tone="up"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-[1.6fr_1fr]">
        <div>
          {chartLoading ? (
            <div className="flex h-[360px] items-center justify-center rounded-lg border border-slate-800 bg-slate-950/40 text-sm text-slate-400">
              Carregando gráfico analisado...
            </div>
          ) : chart && chart.candles.length > 0 ? (
            <CandlestickChart
              candles={chart.candles}
              symbol={approval.symbol}
              timeframe={approval.timeframe || ''}
              overlayLines={overlayLines}
              height={360}
              showVolume={false}
            />
          ) : (
            <div className="flex h-[360px] items-center justify-center rounded-lg border border-slate-800 bg-slate-950/40 text-sm text-slate-500">
              Snapshot do gráfico indisponível para esta aprovação.
            </div>
          )}
        </div>

        <div className="space-y-3">
          <div>
            <h3 className="mb-2 text-sm font-semibold text-white">Critérios do método Didi</h3>
            <div className="flex flex-wrap gap-2">
              {Object.entries(approval.criteria).map(([key, ok]) => (
                <span
                  key={key}
                  className={`rounded-full border px-2 py-0.5 text-xs ${
                    ok
                      ? 'border-emerald-700 bg-emerald-900/30 text-emerald-300'
                      : 'border-slate-700 bg-slate-900/40 text-slate-400'
                  }`}
                >
                  {ok ? '✓' : '✕'} {CRITERIA_LABEL[key] ?? key}
                </span>
              ))}
            </div>
          </div>

          <div>
            <h3 className="mb-2 text-sm font-semibold text-white">Análise</h3>
            <ul className="space-y-1 text-sm leading-relaxed text-slate-300">
              {approval.analysis.length > 0 ? (
                approval.analysis.map((line, idx) => (
                  <li key={idx} className="flex gap-2">
                    <span className="text-slate-600">•</span>
                    <span>{line}</span>
                  </li>
                ))
              ) : (
                <li className="text-slate-500">Sem análise registrada.</li>
              )}
            </ul>
          </div>

          {Object.keys(approval.details).length > 0 && (
            <div>
              <h3 className="mb-2 text-sm font-semibold text-white">Leituras dos indicadores</h3>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-slate-400">
                {Object.entries(approval.details).map(([key, value]) => (
                  <div key={key} className="flex justify-between gap-2">
                    <dt className="uppercase tracking-wide">{key.replace(/_/g, ' ')}</dt>
                    <dd className="font-mono text-slate-200">{value}</dd>
                  </div>
                ))}
              </dl>
            </div>
          )}

          {approval.error && <p className="text-xs text-red-400">Erro: {approval.error}</p>}
        </div>
      </div>
    </article>
  )
}
