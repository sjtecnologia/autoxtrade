'use client'

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type EntryApproval } from '@/lib/api'
import EntryApprovalCard from '@/components/EntryApprovalCard'
import { ToastAlert } from '@/components/ui/ToastAlert'

type Filter = EntryApproval['status'] | 'all'

const FILTERS: Filter[] = ['pending', 'approved', 'executing', 'executed', 'rejected', 'failed', 'all']

export default function ApprovalsPage() {
  const qc = useQueryClient()
  const [toast, setToast] = useState<{ variant: 'success' | 'danger'; msg: string } | null>(null)
  const [filter, setFilter] = useState<Filter>('pending')

  const { data, isLoading } = useQuery({
    queryKey: ['approvals', filter],
    queryFn: () => api.getEntryApprovals(filter === 'all' ? undefined : filter),
    refetchInterval: 15_000,
  })

  const approveMut = useMutation({
    mutationFn: ({ id, quantity }: { id: string; quantity: string }) => api.approveEntry(id, quantity),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['approvals'] })
      setToast({ variant: 'success', msg: 'Entrada autorizada e enviada para execução.' })
    },
    onError: (err: Error) => setToast({ variant: 'danger', msg: `Falha ao aprovar: ${err.message}` }),
  })

  const rejectMut = useMutation({
    mutationFn: (id: string) => api.rejectEntry(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['approvals'] })
      setToast({ variant: 'success', msg: 'Entrada rejeitada.' })
    },
    onError: (err: Error) => setToast({ variant: 'danger', msg: `Falha ao rejeitar: ${err.message}` }),
  })

  const approvals = data ?? []

  return (
    <div className="space-y-6">
      {toast && (
        <ToastAlert
          variant={toast.variant}
          message={toast.msg}
          onDismiss={() => setToast(null)}
          autoDismissMs={3500}
        />
      )}

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Pontos de entrada — método Didi Aguiar</h1>
          <p className="text-sm text-slate-400">
            O robô monitora os ativos, valida os 3 pilares (Didi, DMI/ADX e Bollinger) e pede sua autorização antes de
            operar. Revise o gráfico, a análise e a quantidade sugerida.
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={[
                'rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                filter === f
                  ? 'border-blue-600 bg-blue-600/20 text-blue-300'
                  : 'border-slate-700 text-slate-300 hover:bg-slate-800',
              ].join(' ')}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-8 text-center text-sm text-slate-400">
          Carregando sinais...
        </div>
      ) : approvals.length === 0 ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-8 text-center text-sm text-slate-400">
          Nenhum ponto de entrada para o filtro selecionado. O robô avisa por Telegram assim que um setup aparecer.
        </div>
      ) : (
        <div className="space-y-6">
          {approvals.map((approval) => (
            <EntryApprovalCard
              key={approval.id}
              approval={approval}
              onApprove={(id, quantity) => approveMut.mutate({ id, quantity })}
              onReject={(id) => rejectMut.mutate(id)}
              isSubmitting={approveMut.isPending || rejectMut.isPending}
            />
          ))}
        </div>
      )}
    </div>
  )
}
