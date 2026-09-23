'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export function useDayMetrics(market?: string) {
  return useQuery({
    queryKey: ['day-metrics', market],
    queryFn: () => api.getDailyStats(market),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}
