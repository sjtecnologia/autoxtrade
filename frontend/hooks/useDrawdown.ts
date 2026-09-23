'use client'

import { useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'

export function useDrawdown(market = 'CRIPTO') {
  return useQuery({
    queryKey: ['drawdown', market],
    queryFn: () => api.getDrawdown(market),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}
