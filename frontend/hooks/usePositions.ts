'use client'

import { useEffect, useRef, useState } from 'react'
import { WS_URL } from '@/lib/constants'
import { useBotStore, type Position } from '@/store/botStore'
import { api } from '@/lib/api'

function parseNumber(value: string | number | null | undefined) {
  if (value == null || value === '') return null
  const parsed = typeof value === 'number' ? value : Number.parseFloat(String(value))
  return Number.isFinite(parsed) ? parsed : null
}

function enrichPosition(position: Position, priceMap: Record<string, number>) {
  const entryPrice = parseNumber(position.entry_price)
  const currentPriceFromApi = parseNumber(position.current_price)
  const marketPrice = position.symbol
    ? priceMap[position.symbol.toUpperCase()]
    : null
  const currentPrice = currentPriceFromApi ?? marketPrice ?? null
  const quantity = parseNumber(position.quantity) ?? 1
  const side = position.side?.toUpperCase()
  const isLong = side === 'LONG' || side === 'BUY'
  const sideMultiplier = isLong ? 1 : -1

  let unrealizedPnl = parseNumber(position.unrealized_pnl)
  let unrealizedPnlPct = parseNumber(position.unrealized_pnl_pct)

  if (entryPrice != null && currentPrice != null) {
    const pnl = (currentPrice - entryPrice) * quantity * sideMultiplier
    const pnlPct = entryPrice > 0 ? (pnl / (entryPrice * quantity)) * 100 : null
    unrealizedPnl = pnl
    unrealizedPnlPct = pnlPct
  }

  return {
    ...position,
    current_price: currentPrice == null ? position.current_price : String(currentPrice),
    unrealized_pnl: unrealizedPnl == null ? position.unrealized_pnl : String(unrealizedPnl),
    unrealized_pnl_pct: unrealizedPnlPct == null ? position.unrealized_pnl_pct : String(unrealizedPnlPct),
  }
}

/**
 * Hook que mantém as posições abertas sincronizadas via WebSocket.
 * Fallback: polling REST a cada 2s para manter a UI viva.
 */
export function usePositions() {
  const { positions, setPositions, wsConnected, setWsConnected } = useBotStore()
  const wsRef = useRef<WebSocket | null>(null)
  const priceMapRef = useRef<Record<string, number>>({})
  const [error, setError] = useState<unknown>(null)
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    let cancelled = false

    const loadPositions = async () => {
      try {
        setIsLoading(true)
        const [positionsResult, pricesResult] = await Promise.allSettled([api.getPositions(), api.getPrices()])

        const data = positionsResult.status === 'fulfilled' ? positionsResult.value : []
        const priceMap = pricesResult.status === 'fulfilled'
          ? pricesResult.value.reduce<Record<string, number>>((acc, item) => {
              acc[item.symbol.toUpperCase()] = item.mid
              return acc
            }, {})
          : {}

        priceMapRef.current = priceMap

        if (!cancelled) {
          const enriched = data.map((position) => enrichPosition(position, priceMap))
          setPositions(enriched)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err)
          console.error('Erro ao carregar posições', err)
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false)
        }
      }
    }

    void loadPositions()
    const timer = window.setInterval(() => {
      void loadPositions()
    }, 1_500)

    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [setPositions])

  useEffect(() => {
    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => setWsConnected(true)
    ws.onclose = () => setWsConnected(false)
    ws.onerror = () => setWsConnected(false)

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string)
        if (msg.type === 'POSITION_UPDATE') {
          const payload = Array.isArray(msg.payload) ? msg.payload : []
          const enriched = payload.map((position: Position) => enrichPosition(position, priceMapRef.current))
          setPositions(enriched)
        }
        if (msg.type === 'BOT_STATUS_CHANGED') {
          useBotStore.getState().setBotStatus(
            msg.payload.market,
            msg.payload.status as 'ATIVO' | 'PAUSADO' | 'DESCONECTADO',
          )
        }
      } catch {
        // ignora mensagens não-JSON
      }
    }

    return () => ws.close()
  }, [setPositions, setWsConnected])

  return { positions, wsConnected, error, isLoading }
}
