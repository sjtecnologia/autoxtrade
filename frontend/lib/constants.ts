export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

export const WS_URL =
  process.env.NEXT_PUBLIC_WS_URL ?? 'ws://localhost:8000/ws'

export const API_TOKEN =
  process.env.NEXT_PUBLIC_API_TOKEN ?? ''

export const MARKETS = ['CRIPTO', 'B3', 'FOREX'] as const
export type Market = (typeof MARKETS)[number]
