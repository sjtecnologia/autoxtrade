'use client'

import { useEffect, useMemo, useState } from 'react'
import { api, type OHLCVCandle } from '@/lib/api'
import type { Position } from '@/store/botStore'

interface LiveCandlesProps {
  positions: Position[]
}

type CandlePoint = {
  open: number
  close: number
  high: number
  low: number
}

function parsePrice(value: string | number | null | undefined) {
  if (value == null || value === '') return null
  const parsed = typeof value === 'number' ? value : Number.parseFloat(String(value))
  return Number.isFinite(parsed) ? parsed : null
}

function getDisplaySide(position: Pick<Position, 'side'>) {
  const side = position.side?.toUpperCase()
  if (side === 'LONG' || side === 'BUY') return 'LONG'
  if (side === 'SHORT' || side === 'SELL') return 'SHORT'
  return 'SHORT'
}

function hashSymbol(symbol: string) {
  return Array.from(symbol).reduce((acc, char, index) => acc + char.charCodeAt(0) * (index + 1), 0)
}

function buildSeries(price: number, bias: number, seed: number) {
  const candles: CandlePoint[] = []
  let previous = price * (1 - bias * 0.008)
  const volatility = Math.max(price * 0.004, 0.35)

  for (let index = 0; index < 10; index += 1) {
    const phase = seed / 1000 + index * 0.35
    const move = Math.sin(phase) * volatility + Math.cos(phase * 0.9 + bias) * (volatility * 0.45)
    const open = previous + move * 0.5
    const close = previous + move
    const high = Math.max(open, close) + volatility * 0.35
    const low = Math.min(open, close) - volatility * 0.35
    candles.push({ open, close, high, low })
    previous = close
  }

  return candles
}

function buildSeriesFromOHLCV(ohlcv: OHLCVCandle[]) {
  if (!ohlcv?.length) return null
  return ohlcv.slice(-10).map((candle) => ({
    open: candle.open,
    close: candle.close,
    high: candle.high,
    low: candle.low,
  }))
}

function formatPrice(value: number | null) {
  return value == null ? '—' : `$${value.toFixed(2)}`
}

export function LiveCandles({ positions }: LiveCandlesProps) {
  const [ohlcvMap, setOhlcvMap] = useState<Record<string, CandlePoint[]>>({})

  useEffect(() => {
    let cancelled = false
    const visiblePositions = positions.slice(0, 8)

    if (visiblePositions.length === 0) {
      setOhlcvMap({})
      return () => {
        cancelled = true
      }
    }

    const loadSeries = async () => {
      const next: Record<string, CandlePoint[]> = {}
      await Promise.all(
        visiblePositions.map(async (position) => {
          try {
            const data = await api.getOHLCV(position.symbol, '15m', 'CRIPTO', 10)
            if (!cancelled) {
              const candles = buildSeriesFromOHLCV(data)
              if (candles) {
                next[position.symbol.toUpperCase()] = candles
              }
            }
          } catch {
            // fallback para a série sintética abaixo
          }
        }),
      )

      if (!cancelled) {
        setOhlcvMap(next)
      }
    }

    void loadSeries()

    return () => {
      cancelled = true
    }
  }, [positions])

  const seriesByPosition = useMemo(() => {
    const fallbackPositions: Position[] = positions.length > 0
      ? positions
      : [
          {
            id: 1,
            symbol: 'BTCUSD',
            side: 'LONG',
            mode: 'paper',
            entry_price: '100',
            current_price: '105',
            quantity: '0.25',
            unrealized_pnl: '1.25',
            unrealized_pnl_pct: '1.25',
            stop_loss: '95',
            take_profit: null,
            open_at: new Date().toISOString(),
          } as Position,
          {
            id: 2,
            symbol: 'ETHUSD',
            side: 'SHORT',
            mode: 'paper',
            entry_price: '3200',
            current_price: '3150',
            quantity: '1.5',
            unrealized_pnl: '-75',
            unrealized_pnl_pct: '-2.34',
            stop_loss: '3300',
            take_profit: null,
            open_at: new Date().toISOString(),
          } as Position,
        ]

    return fallbackPositions.slice(0, 8).map((position) => {
      const basePrice = parsePrice(position.current_price ?? position.entry_price) ?? parsePrice(position.entry_price) ?? 0
      const bias = getDisplaySide(position) === 'LONG' ? 1 : -1
      const symbolKey = position.symbol?.toUpperCase() ?? 'fallback'
      const candles = ohlcvMap[symbolKey] ?? buildSeries(basePrice, bias, hashSymbol(symbolKey))
      return {
        position,
        candles,
      }
    })
  }, [ohlcvMap, positions])

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
      {seriesByPosition.map(({ position, candles }) => {
        const prices = candles.flatMap((candle) => [candle.low, candle.high])
        const min = Math.min(...prices)
        const max = Math.max(...prices)
        const range = max - min || 1
        const currentPrice = parsePrice(position.current_price ?? position.entry_price)

        const y = (value: number) => 48 - ((value - min) / range) * 34

        return (
          <div key={position.id} className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <div>
                <div className="text-sm font-semibold text-white">{position.symbol}</div>
                <div className={`text-[11px] ${position.side === 'LONG' ? 'text-emerald-400' : 'text-red-400'}`}>
                  {position.side === 'LONG' ? 'Long' : 'Short'}
                </div>
              </div>
              <div className="text-right">
                <div className="font-mono text-sm text-white">{formatPrice(currentPrice)}</div>
                <div className="text-[10px] text-slate-500">Qty {position.quantity}</div>
              </div>
            </div>

            <svg viewBox="0 0 120 60" className="h-20 w-full">
              {[0, 20, 40, 60].map((lineY) => (
                <line key={lineY} x1="0" y1={lineY + 6} x2="120" y2={lineY + 6} stroke="#334155" strokeDasharray="2 2" />
              ))}
              {candles.map((candle, index) => {
                const x = 8 + index * 11
                const openY = y(candle.open)
                const closeY = y(candle.close)
                const highY = y(candle.high)
                const lowY = y(candle.low)
                const isUp = candle.close >= candle.open
                const candleHeight = Math.max(4, Math.abs(candle.close - candle.open) / range * 24)
                const candleTop = Math.min(openY, closeY)

                return (
                  <g key={`${position.id}-${index}`}>
                    <line x1={x + 2} y1={highY} x2={x + 2} y2={lowY} stroke={isUp ? '#34d399' : '#f87171'} strokeWidth="1" />
                    <rect
                      x={x}
                      y={candleTop}
                      width="6"
                      height={candleHeight}
                      rx="1"
                      fill={isUp ? '#34d399' : '#f87171'}
                      opacity="0.9"
                    />
                  </g>
                )
              })}
            </svg>
          </div>
        )
      })}
    </div>
  )
}
