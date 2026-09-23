'use client'

import { useEffect, useRef, useLayoutEffect } from 'react'
import {
  createChart,
  createSeriesMarkers,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type HistogramData,
  type SeriesMarker,
  type Time,
  type DeepPartial,
  type ChartOptions,
} from 'lightweight-charts'
import type { OHLCVCandle, TradeSummary } from '@/lib/api'

interface Props {
  candles: OHLCVCandle[]
  trades?: TradeSummary[]
  symbol: string
  timeframe: string
  overlayLines?: OverlayLine[]
  paneHeights?: number[]
  height?: number
  showCandles?: boolean
  showVolume?: boolean
  onCrosshairTimeChange?: (time: number | null) => void
  syncCrosshairTime?: number | null
}

interface OverlayLine {
  id: string
  label: string
  color: string
  priceScaleId?: 'left' | 'right'
  paneIndex?: number
  data: Array<{ time: number; value: number }>
}

interface OverlaySeriesState {
  series: ISeriesApi<'Line'>
  paneIndex: number
  priceScaleId: 'left' | 'right'
}

const CHART_COLORS = {
  bg: '#0f172a',
  grid: '#1e293b',
  text: '#94a3b8',
  upColor: '#22c55e',
  downColor: '#ef4444',
  wickUp: '#16a34a',
  wickDown: '#dc2626',
  volume: '#3b82f620',
  volumeUp: '#22c55e40',
  volumeDown: '#ef444440',
}

export default function CandlestickChart({
  candles,
  trades = [],
  symbol,
  timeframe,
  overlayLines = [],
  paneHeights = [],
  height = 480,
  showCandles = true,
  showVolume = true,
  onCrosshairTimeChange,
  syncCrosshairTime = null,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const markerApiRef = useRef<{ setMarkers: (m: SeriesMarker<Time>[]) => void } | null>(null)
  const overlaySeriesRef = useRef<Map<string, OverlaySeriesState>>(new Map())
  const onCrosshairTimeChangeRef = useRef<Props['onCrosshairTimeChange']>(onCrosshairTimeChange)

  useEffect(() => {
    onCrosshairTimeChangeRef.current = onCrosshairTimeChange
  }, [onCrosshairTimeChange])

  // Cria o gráfico uma única vez
  useLayoutEffect(() => {
    if (!containerRef.current) return

    const chartOptions: DeepPartial<ChartOptions> = {
      layout: {
        background: { type: ColorType.Solid, color: CHART_COLORS.bg },
        textColor: CHART_COLORS.text,
        fontSize: 11,
      },
      grid: {
        vertLines: { color: CHART_COLORS.grid },
        horzLines: { color: CHART_COLORS.grid },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: CHART_COLORS.grid,
        scaleMargins: { top: 0.05, bottom: 0.25 },
      },
      leftPriceScale: {
        visible: false,
        borderColor: CHART_COLORS.grid,
      },
      timeScale: {
        borderColor: CHART_COLORS.grid,
        timeVisible: true,
        secondsVisible: false,
      },
      width: containerRef.current.clientWidth,
      height,
    }

    const chart = createChart(containerRef.current, chartOptions)
    chartRef.current = chart

    const crosshairMoveHandler = (param: { time?: Time; point?: { x: number; y: number } }) => {
      const cb = onCrosshairTimeChangeRef.current
      if (!cb) return

      if (!param.point || param.time === undefined) {
        cb(null)
        return
      }

      if (typeof param.time === 'number') {
        cb(param.time)
        return
      }

      cb(null)
    }
    chart.subscribeCrosshairMove(crosshairMoveHandler)

    if (showCandles) {
      // Série de candlestick (v5: addSeries)
      const candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: CHART_COLORS.upColor,
        downColor: CHART_COLORS.downColor,
        wickUpColor: CHART_COLORS.wickUp,
        wickDownColor: CHART_COLORS.wickDown,
        borderVisible: false,
      })
      candleSeriesRef.current = candleSeries
      markerApiRef.current = createSeriesMarkers(candleSeries as ISeriesApi<'Candlestick', Time>, [])
    } else {
      candleSeriesRef.current = null
      markerApiRef.current = null
    }

    if (showCandles && showVolume) {
      // Série de volume (sobreposta na parte inferior)
      const volumeSeries = chart.addSeries(HistogramSeries, {
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
      })
      chart.priceScale('volume').applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
      })
      volumeSeriesRef.current = volumeSeries
    } else {
      volumeSeriesRef.current = null
    }

    // Resize observer
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        chart.applyOptions({ width: entry.contentRect.width })
      }
    })
    observer.observe(containerRef.current)

    return () => {
      chart.unsubscribeCrosshairMove(crosshairMoveHandler)
      observer.disconnect()
      chart.remove()
      chartRef.current = null
      candleSeriesRef.current = null
      volumeSeriesRef.current = null
      markerApiRef.current = null
      overlaySeriesRef.current.clear()
    }
  }, [height, showCandles, showVolume])

  // Atualiza dados quando candles mudam
  useEffect(() => {
    const candleSeries = candleSeriesRef.current
    const volumeSeries = volumeSeriesRef.current
    const markerApi = markerApiRef.current

    if (!candleSeries && overlayLines.length === 0) return

    // Quando não há candles para o ativo/timeframe selecionado,
    // limpamos o gráfico para não manter desenho do ativo anterior.
    if (candles.length === 0) {
      candleSeries?.setData([])
      volumeSeries?.setData([])
      markerApi?.setMarkers([])
      return
    }

    const candleData: CandlestickData[] = candles.map((c) => ({
      time: c.time as Time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }))

    const volumeData: HistogramData[] = candles.map((c) => ({
      time: c.time as Time,
      value: c.volume,
      color: c.close >= c.open ? CHART_COLORS.volumeUp : CHART_COLORS.volumeDown,
    }))

    candleSeries?.setData(candleData)
    volumeSeries?.setData(volumeData)

    // Adiciona markers de trades (entradas e saídas)
    if (markerApi && trades.length > 0) {
      const markers: SeriesMarker<Time>[] = []

      for (const trade of trades) {
        const entryTs = Math.floor(new Date(trade.open_at).getTime() / 1000) as Time
        const isLong = trade.side === 'BUY' || trade.side === 'LONG'

        markers.push({
          time: entryTs,
          position: isLong ? 'belowBar' : 'aboveBar',
          color: isLong ? '#22c55e' : '#ef4444',
          shape: isLong ? 'arrowUp' : 'arrowDown',
          text: `${isLong ? '▲' : '▼'} ${trade.symbol}`,
          size: 1,
        })

        if (trade.close_at && trade.exit_price) {
          const exitTs = Math.floor(new Date(trade.close_at).getTime() / 1000) as Time
          const pnl = parseFloat(trade.pnl_net ?? '0')
          markers.push({
            time: exitTs,
            position: isLong ? 'aboveBar' : 'belowBar',
            color: pnl >= 0 ? '#22c55e' : '#ef4444',
            shape: 'circle',
            text: `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`,
            size: 0.5,
          })
        }
      }

      // Ordena markers por tempo (obrigatório para lightweight-charts)
      markers.sort((a, b) => (a.time as number) - (b.time as number))
      markerApi.setMarkers(markers)
    } else {
      markerApi?.setMarkers([])
    }

    // Scroll para mostrar as últimas barras
    chartRef.current?.timeScale().scrollToRealTime()
  }, [candles, trades, overlayLines])

  // Gerencia séries sobrepostas (SMA, Didi, etc).
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    const current = overlaySeriesRef.current
    const nextIds = new Set(overlayLines.map((line) => line.id))

    // Remove apenas séries que saíram da configuração atual.
    current.forEach((state, id) => {
      if (!nextIds.has(id)) {
        chart.removeSeries(state.series)
        current.delete(id)
      }
    })

    for (const line of overlayLines) {
      const paneIndex = line.paneIndex ?? 0
      const priceScaleId = line.priceScaleId ?? 'right'
      const existing = current.get(line.id)
      const shouldRecreate =
        !existing ||
        existing.paneIndex !== paneIndex ||
        existing.priceScaleId !== priceScaleId

      if (shouldRecreate && existing) {
        chart.removeSeries(existing.series)
        current.delete(line.id)
      }

      if (shouldRecreate) {
        const createdSeries = chart.addSeries(LineSeries, {
          color: line.color,
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: true,
          priceScaleId,
        }, paneIndex)
        current.set(line.id, {
          series: createdSeries,
          paneIndex,
          priceScaleId,
        })
      }

      const state = current.get(line.id)
      if (!state) continue

      state.series.applyOptions({
        color: line.color,
      })

      state.series.setData(
        line.data.map((p) => ({
          time: p.time as Time,
          value: p.value,
        }))
      )
    }

    const hasLeftScaleOverlay = overlayLines.some((line) => line.priceScaleId === 'left')
    chart.applyOptions({
      leftPriceScale: {
        visible: hasLeftScaleOverlay,
        borderColor: CHART_COLORS.grid,
      },
    })
  }, [overlayLines])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || paneHeights.length === 0) return

    const panes = chart.panes()
    const paneCount = Math.min(paneHeights.length, panes.length)
    if (paneCount === 0) return

    const containerHeight = containerRef.current?.clientHeight ?? height
    const weights = paneHeights
      .slice(0, paneCount)
      .map((size) => Math.max(1, Math.floor(size)))
    const totalWeight = weights.reduce((acc, w) => acc + w, 0)

    const normalizedHeights = weights.map((w) => Math.max(40, Math.floor((w / totalWeight) * containerHeight)))
    const used = normalizedHeights.reduce((acc, h) => acc + h, 0)
    const remainder = containerHeight - used

    if (remainder !== 0) {
      const step = remainder > 0 ? 1 : -1
      for (let i = 0; i < Math.abs(remainder); i += 1) {
        const idx = i % normalizedHeights.length
        const candidate = normalizedHeights[idx] + step
        normalizedHeights[idx] = Math.max(40, candidate)
      }
    }

    normalizedHeights.forEach((size, idx) => {
      const pane = panes[idx]
      if (!pane) return
      pane.setHeight(Math.max(40, Math.floor(size)))
    })
  }, [paneHeights, overlayLines, height])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    if (syncCrosshairTime === null) {
      chart.clearCrosshairPosition()
      return
    }

    const crosshairSeries = candleSeriesRef.current ?? Array.from(overlaySeriesRef.current.values())[0]?.series
    if (!crosshairSeries) return

    const pickNearestValue = (points: Array<{ time: number; value: number }>, t: number): number | null => {
      if (points.length === 0) return null

      let left = 0
      let right = points.length - 1
      while (left <= right) {
        const mid = Math.floor((left + right) / 2)
        const midTime = points[mid].time
        if (midTime === t) return points[mid].value
        if (midTime < t) {
          left = mid + 1
        } else {
          right = mid - 1
        }
      }

      const leftPoint = points[Math.min(points.length - 1, Math.max(0, left))]
      const rightPoint = points[Math.min(points.length - 1, Math.max(0, right))]
      if (!leftPoint) return rightPoint?.value ?? null
      if (!rightPoint) return leftPoint?.value ?? null

      return Math.abs(leftPoint.time - t) < Math.abs(rightPoint.time - t)
        ? leftPoint.value
        : rightPoint.value
    }

    let syncPrice: number | null = null

    if (showCandles && candles.length > 0) {
      const candlePoints = candles.map((c) => ({ time: c.time, value: c.close }))
      syncPrice = pickNearestValue(candlePoints, syncCrosshairTime)
    }

    if (syncPrice === null && overlayLines.length > 0) {
      for (const line of overlayLines) {
        syncPrice = pickNearestValue(line.data, syncCrosshairTime)
        if (syncPrice !== null) break
      }
    }

    if (syncPrice === null || !Number.isFinite(syncPrice)) return
    chart.setCrosshairPosition(syncPrice, syncCrosshairTime as Time, crosshairSeries)
  }, [syncCrosshairTime, candles, overlayLines, showCandles])

  return (
    <div
      style={{ height }}
      className="relative w-full rounded-lg overflow-hidden border border-slate-700 bg-slate-900"
    >
      <div ref={containerRef} className="absolute inset-0" />
    </div>
  )
}
