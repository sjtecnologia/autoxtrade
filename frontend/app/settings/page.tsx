'use client'

import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { api, type BotConfig } from '@/lib/api'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { ConfirmModal } from '@/components/ui/ConfirmModal'
import { ToastAlert } from '@/components/ui/ToastAlert'
import { DataTable, type Column } from '@/components/ui/DataTable'
import { formatDate } from '@/lib/formatters'
import { MARKETS } from '@/lib/constants'

const riskSchema = z.object({
  risk_per_trade_pct: z.number().min(0.005).max(0.05),
  max_open_trades: z.number().int().min(1).max(10),
  max_drawdown_pct: z.number().min(5).max(30),
  drawdown_alert_pct: z.number().min(3).max(25),
  max_corr_threshold: z.number().min(0.3).max(1.0),
})

type RiskFormValues = z.infer<typeof riskSchema>

interface AuditRow {
  id: number
  market: string
  field: string
  old_value: string | null
  new_value: string | null
  changed_at: string
}

function MarketCard({ market }: { market: string }) {
  const qc = useQueryClient()
  const [modal, setModal] = useState<'pause' | 'resume' | null>(null)
  const [toast, setToast] = useState<{ variant: 'success' | 'danger'; msg: string } | null>(null)

  const { data: cfg, isLoading } = useQuery<BotConfig>({
    queryKey: ['config', market],
    queryFn: () => api.getConfig(market),
    refetchInterval: 30_000,
  })

  const { data: auditLog, isLoading: auditLoading } = useQuery<AuditRow[]>({
    queryKey: ['audit-log', market],
    queryFn: () => api.getAuditLog(market, 10),
    refetchInterval: 60_000,
  })

  const pauseMut = useMutation({
    mutationFn: () => api.pauseBot(market),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['config', market] })
      qc.invalidateQueries({ queryKey: ['audit-log', market] })
      setToast({ variant: 'success', msg: `Bot ${market} pausado.` })
    },
    onError: () => setToast({ variant: 'danger', msg: 'Erro ao pausar o bot.' }),
  })

  const resumeMut = useMutation({
    mutationFn: () => api.resumeBot(market),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['config', market] })
      qc.invalidateQueries({ queryKey: ['audit-log', market] })
      setToast({ variant: 'success', msg: `Bot ${market} retomado.` })
    },
    onError: () => setToast({ variant: 'danger', msg: 'Erro ao retomar o bot.' }),
  })

  const patchMut = useMutation({
    mutationFn: (data: RiskFormValues) => api.patchConfig(market, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['config', market] })
      qc.invalidateQueries({ queryKey: ['audit-log', market] })
      setToast({ variant: 'success', msg: 'Configurações salvas.' })
    },
    onError: () => setToast({ variant: 'danger', msg: 'Erro ao salvar configurações.' }),
  })

  const importSymbolsMut = useMutation({
    mutationFn: () => api.importSymbols(market),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['config', market] })
      qc.invalidateQueries({ queryKey: ['audit-log', market] })
      setToast({ variant: 'success', msg: `${res.count} papéis importados para ${market}.` })
    },
    onError: (err: Error) => {
      setToast({ variant: 'danger', msg: `Erro ao importar papéis: ${err.message}` })
    },
  })

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<RiskFormValues>({
    resolver: zodResolver(riskSchema),
  })

  useEffect(() => {
    if (cfg) {
      reset({
        risk_per_trade_pct: parseFloat(cfg.risk_per_trade_pct),
        max_open_trades: cfg.max_open_trades,
        max_drawdown_pct: parseFloat(cfg.max_drawdown_pct),
        drawdown_alert_pct: parseFloat(cfg.drawdown_alert_pct),
        max_corr_threshold: parseFloat(cfg.max_corr_threshold),
      })
    }
  }, [cfg, reset])

  const auditColumns: Column<AuditRow>[] = [
    { key: 'field', header: 'Campo' },
    {
      key: 'old_value',
      header: 'Anterior',
      render: (r) => <span className="font-mono text-xs text-slate-400">{r.old_value ?? '—'}</span>,
    },
    {
      key: 'new_value',
      header: 'Novo',
      render: (r) => <span className="font-mono text-xs text-slate-300">{r.new_value ?? '—'}</span>,
    },
    { key: 'changed_at', header: 'Data', render: (r) => formatDate(r.changed_at) },
  ]

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-6 space-y-6">
      {toast && (
        <ToastAlert
          variant={toast.variant}
          message={toast.msg}
          onDismiss={() => setToast(null)}
          autoDismissMs={3000}
        />
      )}

      {/* Header do mercado */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">{market}</h2>
          {cfg && !isLoading && (
            <p className="text-xs text-slate-400 mt-0.5">
              {cfg.exchange} · {cfg.mode}
            </p>
          )}
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={() => importSymbolsMut.mutate()}
            disabled={importSymbolsMut.isPending}
            className="rounded-lg border border-blue-700 px-3 py-1.5 text-sm text-blue-300 hover:bg-blue-900/30 disabled:opacity-40 transition-colors"
          >
            {importSymbolsMut.isPending ? 'Importando papéis...' : 'Importar papéis da corretora'}
          </button>
          {cfg && <StatusBadge status={cfg.is_active ? 'ATIVO' : 'PAUSADO'} />}
          {cfg?.is_active ? (
            <button
              onClick={() => setModal('pause')}
              className="rounded-lg border border-yellow-600 px-3 py-1.5 text-sm text-yellow-400 hover:bg-yellow-900/30 transition-colors"
            >
              Pausar
            </button>
          ) : (
            <button
              onClick={() => setModal('resume')}
              className="rounded-lg border border-emerald-600 px-3 py-1.5 text-sm text-emerald-400 hover:bg-emerald-900/30 transition-colors"
            >
              Retomar
            </button>
          )}
        </div>
      </div>

      {/* Formulário de risco */}
      <form
        onSubmit={handleSubmit((data) => patchMut.mutate(data))}
        className="space-y-4"
      >
        <h3 className="text-sm font-semibold text-slate-400 uppercase tracking-wide">
          Parâmetros de Risco
        </h3>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Field
            label="Risco por Trade (%)"
            hint="0.5% – 5%"
            error={errors.risk_per_trade_pct?.message}
          >
            <input
              type="number"
              step="0.001"
              min="0.005"
              max="0.05"
              {...register('risk_per_trade_pct', { valueAsNumber: true })}
              className="input-base"
            />
          </Field>

          <Field
            label="Máx. Posições Abertas"
            hint="1 – 10"
            error={errors.max_open_trades?.message}
          >
            <input
              type="number"
              step="1"
              min="1"
              max="10"
              {...register('max_open_trades', { valueAsNumber: true })}
              className="input-base"
            />
          </Field>

          <Field
            label="Drawdown Máximo (%)"
            hint="5% – 30%"
            error={errors.max_drawdown_pct?.message}
          >
            <input
              type="number"
              step="0.5"
              min="5"
              max="30"
              {...register('max_drawdown_pct', { valueAsNumber: true })}
              className="input-base"
            />
          </Field>

          <Field
            label="Alerta Drawdown (%)"
            hint="3% – 25%"
            error={errors.drawdown_alert_pct?.message}
          >
            <input
              type="number"
              step="0.5"
              min="3"
              max="25"
              {...register('drawdown_alert_pct', { valueAsNumber: true })}
              className="input-base"
            />
          </Field>

          <Field
            label="Corr. Máxima"
            hint="0.3 – 1.0"
            error={errors.max_corr_threshold?.message}
          >
            <input
              type="number"
              step="0.01"
              min="0.3"
              max="1.0"
              {...register('max_corr_threshold', { valueAsNumber: true })}
              className="input-base"
            />
          </Field>
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <button
            type="button"
            onClick={() => cfg && reset({
              risk_per_trade_pct: parseFloat(cfg.risk_per_trade_pct),
              max_open_trades: cfg.max_open_trades,
              max_drawdown_pct: parseFloat(cfg.max_drawdown_pct),
              drawdown_alert_pct: parseFloat(cfg.drawdown_alert_pct),
              max_corr_threshold: parseFloat(cfg.max_corr_threshold),
            })}
            disabled={!isDirty}
            className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-400 hover:bg-slate-800 disabled:opacity-40 transition-colors"
          >
            Descartar
          </button>
          <button
            type="submit"
            disabled={!isDirty || patchMut.isPending}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-40 transition-colors"
          >
            {patchMut.isPending ? 'Salvando...' : 'Salvar'}
          </button>
        </div>
      </form>

      {/* Audit log */}
      <div>
        <h3 className="mb-3 text-sm font-semibold text-slate-400 uppercase tracking-wide">
          Últimas alterações
        </h3>
        <DataTable
          columns={auditColumns}
          data={auditLog ?? []}
          isLoading={auditLoading}
          emptyMessage="Sem alterações registradas"
          keyField="id"
        />
      </div>

      <ConfirmModal
        isOpen={modal === 'pause'}
        title={`Pausar bot ${market}?`}
        message="O bot irá parar de abrir novas posições. As posições abertas continuam até o fechamento natural."
        confirmLabel="Pausar"
        destructive
        onConfirm={() => { setModal(null); pauseMut.mutate() }}
        onCancel={() => setModal(null)}
      />
      <ConfirmModal
        isOpen={modal === 'resume'}
        title={`Retomar bot ${market}?`}
        message="O bot voltará a operar normalmente de acordo com os sinais do ML."
        confirmLabel="Retomar"
        onConfirm={() => { setModal(null); resumeMut.mutate() }}
        onCancel={() => setModal(null)}
      />
    </div>
  )
}

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string
  hint?: string
  error?: string
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-medium text-slate-400">
        {label}
        {hint && <span className="ml-1 text-slate-600">({hint})</span>}
      </label>
      {children}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  )
}

export default function SettingsPage() {
  const qc = useQueryClient()
  const [toast, setToast] = useState<{ variant: 'success' | 'danger'; msg: string } | null>(null)

  const importWatchMut = useMutation({
    mutationFn: () => api.importSymbolsFromMarketWatch(),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['audit-log'] })
      const total = Object.values(res.imported).reduce((acc, cur) => acc + cur.count, 0)
      setToast({
        variant: 'success',
        msg: `Market Watch sincronizado. ${total} papéis importados (${res.feed_symbols_count} no feed live).`,
      })
    },
    onError: (err: Error) => {
      setToast({ variant: 'danger', msg: `Erro na sincronização: ${err.message}` })
    },
  })

  const importAutogenMut = useMutation({
    mutationFn: () => api.importSymbolsAutogen(),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['config'] })
      qc.invalidateQueries({ queryKey: ['audit-log'] })
      const total = Object.values(res.imported).reduce((acc, cur) => acc + cur.count, 0)
      setToast({
        variant: 'success',
        msg: `Arquivo gerado automaticamente (${res.generated_symbols_count}) e importação concluída: ${total} papéis.`,
      })
    },
    onError: (err: Error) => {
      setToast({ variant: 'danger', msg: `Erro no fluxo automático: ${err.message}` })
    },
  })

  return (
    <div className="space-y-6">
      {toast && (
        <ToastAlert
          variant={toast.variant}
          message={toast.msg}
          onDismiss={() => setToast(null)}
          autoDismissMs={4000}
        />
      )}
      <div>
        <h1 className="text-2xl font-bold text-white">Configurações</h1>
        <p className="text-sm text-slate-400">Parâmetros de risco e controles dos bots</p>
        <div className="mt-3">
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => importAutogenMut.mutate()}
              disabled={importAutogenMut.isPending}
              className="rounded-lg border border-indigo-700 px-4 py-2 text-sm text-indigo-300 hover:bg-indigo-900/30 disabled:opacity-40 transition-colors"
            >
              {importAutogenMut.isPending
                ? 'Gerando arquivo e importando...'
                : 'Gerar arquivo automaticamente e importar tudo'}
            </button>

            <button
              onClick={() => importWatchMut.mutate()}
              disabled={importWatchMut.isPending}
              className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 hover:bg-slate-800/60 disabled:opacity-40 transition-colors"
            >
              {importWatchMut.isPending
                ? 'Sincronizando Market Watch...'
                : 'Sincronizar Market Watch e importar tudo'}
            </button>
          </div>
        </div>
      </div>
      {MARKETS.map((m) => (
        <MarketCard key={m} market={m} />
      ))}
    </div>
  )
}
