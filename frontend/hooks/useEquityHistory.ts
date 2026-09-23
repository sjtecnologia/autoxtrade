'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export function useEquityHistory(market = 'CRIPTO', hours = 24) {
  return useQuery({
    queryKey: ['equity-history', market, hours],
    queryFn: () => api.getEquityHistory(market, hours),
    refetchInterval: 300_000, // 5 min
    staleTime: 120_000,
  })
}
