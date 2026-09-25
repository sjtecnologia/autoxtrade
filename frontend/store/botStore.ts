'use client'

import { create } from 'zustand'

export interface Position {
  id: number
  symbol: string
  side: string
  mode: string
  entry_price: string
  current_price: string
  quantity: string
  unrealized_pnl: string
  unrealized_pnl_pct: string
  stop_loss: string
  take_profit: string | null
  open_at: string
}

interface BotStore {
  positions: Position[]
  botStatus: Record<string, 'ATIVO' | 'PAUSADO' | 'DESCONECTADO'>
  wsConnected: boolean
  setPositions: (positions: Position[]) => void
  setBotStatus: (market: string, status: 'ATIVO' | 'PAUSADO' | 'DESCONECTADO') => void
  setWsConnected: (connected: boolean) => void
}

export const useBotStore = create<BotStore>((set) => ({
  positions: [],
  botStatus: {},
  wsConnected: false,
  setPositions: (positions) => set({ positions }),
  setBotStatus: (market, status) =>
    set((state) => ({ botStatus: { ...state.botStatus, [market]: status } })),
  setWsConnected: (wsConnected) => set({ wsConnected }),
}))
