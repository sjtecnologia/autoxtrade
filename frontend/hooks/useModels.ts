'use client'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type OptimizeTimeframesRequest, type TrainRequest } from '@/lib/api'

export function useModels(market?: string) {
  return useQuery({
    queryKey: ['models', market],
    queryFn: () => api.getModels(market),
    refetchInterval: 15_000,
    staleTime: 10_000,
  })
}

export function useTriggerTrain() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: TrainRequest) => api.triggerTrain(body),
    onSuccess: () => {
      // Recarrega lista de modelos após iniciar treinamento
      setTimeout(() => queryClient.invalidateQueries({ queryKey: ['models'] }), 3_000)
    },
  })
}

export function useTrainJobStatus(jobId: string | null) {
  return useQuery({
    queryKey: ['train-job-status', jobId],
    queryFn: () => api.getTrainStatus(jobId as string),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'queued' || status === 'running' ? 3000 : false
    },
    staleTime: 1000,
  })
}

export function useTriggerOptimizeTimeframes() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: OptimizeTimeframesRequest) => api.triggerOptimizeTimeframes(body),
    onSuccess: () => {
      setTimeout(() => queryClient.invalidateQueries({ queryKey: ['models'] }), 3_000)
    },
  })
}
