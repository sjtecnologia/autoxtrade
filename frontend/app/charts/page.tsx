'use client'

import { useState, useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import dynamic from 'next/dynamic'
import { api, type OHLCVCandle, type TradeSummary } from '@/lib/api'

// Import dinâmico — lightweight-charts usa window (só funciona no browser)
const CandlestickChart = dynamic(() => import('@/components/CandlestickChart'), {
  ssr: false,
  loading: () => (
    <div className="w-full h-[480px] rounded-lg border border-slate-700 bg-slate-800/50 flex items-center justify-center">
      <span className="text-slate-400 text-sm animate-pulse">Carregando gráfico...</span>
    </div>
  ),
})

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
const LIMITS: Record<string, number> = {
  '1m': 500, '5m': 500, '15m': 500, '30m': 500,
  '1h': 500, '4h': 300, '1d': 200,
}

const TF_SECONDS: Record<string, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '4h': 14400,
  '1d': 86400,
}

const EMPTY_SYMBOL_MAP: Record<string, string[]> = {}
const EMPTY_CANDLES: OHLCVCandle[] = []

function alignToTimeframe(tsSec: number, timeframe: string) {
  const step = TF_SECONDS[timeframe] ?? 3600
  return Math.floor(tsSec / step) * step
}

function fmt(n: number, digits = 5) {
  return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

function pctColor(v: number) {
  if (v > 0) return 'text-emerald-400'
  if (v < 0) return 'text-red-400'
  return 'text-slate-400'
}

type OverlayLine = {
  id: string
  label: string
  color: string
  priceScaleId?: 'left' | 'right'
  paneIndex?: number
  data: Array<{ time: number; value: number }>
}

type PaneLegend = {
  paneIndex: number
  title: string
  lines: Array<{
    label: string
    color: string
  }>
}

type IndicatorSettings = {
  showSMA: boolean
  smaPeriod: number
  showEMA: boolean
  emaPeriod: number
  showDidi: boolean
  didiFastPeriod: number
  didiMidPeriod: number
  didiSlowPeriod: number
  showBollinger: boolean
  bollingerPeriod: number
  bollingerStdDev: number
  showDMI: boolean
  dmiPeriod: number
  showStochastic: boolean
  stochasticKPeriod: number
  stochasticDPeriod: number
  showTRIX: boolean
  trixPeriod: number
  trixSignalPeriod: number
}

type CustomIndicatorPreset = {
  id: string
  name: string
  settings: IndicatorSettings
  updatedAt: string
}

const CUSTOM_PRESETS_KEY = 'chart:custom-indicator-presets:v1'

type PresetsExportPayload = {
  version: 1
  exportedAt: string
  presets: CustomIndicatorPreset[]
}

const DEFAULT_INDICATOR_SETTINGS: IndicatorSettings = {
  showSMA: true,
  smaPeriod: 20,
  showEMA: false,
  emaPeriod: 9,
  showDidi: false,
  didiFastPeriod: 3,
  didiMidPeriod: 8,
  didiSlowPeriod: 20,
  showBollinger: false,
  bollingerPeriod: 20,
  bollingerStdDev: 2,
  showDMI: false,
  dmiPeriod: 14,
  showStochastic: false,
  stochasticKPeriod: 14,
  stochasticDPeriod: 3,
  showTRIX: false,
  trixPeriod: 9,
  trixSignalPeriod: 9,
}

function clampPositiveInt(value: number, fallback: number) {
  if (!Number.isFinite(value)) return fallback
  const n = Math.floor(value)
  return n > 0 ? n : fallback
}

function smaValues(values: number[], period: number): Array<number | null> {
  const p = clampPositiveInt(period, 9)
  const result: Array<number | null> = Array(values.length).fill(null)
  if (values.length < p) return result

  let sum = 0
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i]
    if (i >= p) {
      sum -= values[i - p]
    }
    if (i >= p - 1) {
      result[i] = sum / p
    }
  }
  return result
}

function emaValues(values: number[], period: number): Array<number | null> {
  const p = clampPositiveInt(period, 9)
  const result: Array<number | null> = Array(values.length).fill(null)
  if (values.length < p) return result

  const k = 2 / (p + 1)
  let seed = 0
  for (let i = 0; i < p; i += 1) {
    seed += values[i]
  }
  let ema = seed / p
  result[p - 1] = ema

  for (let i = p; i < values.length; i += 1) {
    ema = values[i] * k + ema * (1 - k)
    result[i] = ema
  }

  return result
}

function stdDev(values: number[]) {
  if (values.length === 0) return 0
  const mean = values.reduce((acc, v) => acc + v, 0) / values.length
  const variance = values.reduce((acc, v) => acc + ((v - mean) ** 2), 0) / values.length
  return Math.sqrt(variance)
}

function bollingerValues(values: number[], period: number, mult: number) {
  const p = clampPositiveInt(period, 20)
  const result = Array.from({ length: values.length }, () => ({
    mid: null as number | null,
    upper: null as number | null,
    lower: null as number | null,
  }))

  if (values.length < p) return result
  for (let i = p - 1; i < values.length; i += 1) {
    const window = values.slice(i - p + 1, i + 1)
    const mid = window.reduce((acc, v) => acc + v, 0) / p
    const sd = stdDev(window)
    result[i] = {
      mid,
      upper: mid + (mult * sd),
      lower: mid - (mult * sd),
    }
  }
  return result
}

function dmiValues(highs: number[], lows: number[], closes: number[], period: number) {
  const p = clampPositiveInt(period, 14)
  const n = closes.length
  const plusDI: Array<number | null> = Array(n).fill(null)
  const minusDI: Array<number | null> = Array(n).fill(null)
  const adx: Array<number | null> = Array(n).fill(null)
  if (n < p + 2) return { plusDI, minusDI, adx }

  const tr: number[] = Array(n).fill(0)
  const plusDM: number[] = Array(n).fill(0)
  const minusDM: number[] = Array(n).fill(0)

  for (let i = 1; i < n; i += 1) {
    const upMove = highs[i] - highs[i - 1]
    const downMove = lows[i - 1] - lows[i]
    plusDM[i] = upMove > downMove && upMove > 0 ? upMove : 0
    minusDM[i] = downMove > upMove && downMove > 0 ? downMove : 0

    const hl = highs[i] - lows[i]
    const hc = Math.abs(highs[i] - closes[i - 1])
    const lc = Math.abs(lows[i] - closes[i - 1])
    tr[i] = Math.max(hl, hc, lc)
  }

  let trSmooth = tr.slice(1, p + 1).reduce((acc, v) => acc + v, 0)
  let plusSmooth = plusDM.slice(1, p + 1).reduce((acc, v) => acc + v, 0)
  let minusSmooth = minusDM.slice(1, p + 1).reduce((acc, v) => acc + v, 0)
  const dx: Array<number | null> = Array(n).fill(null)

  for (let i = p; i < n; i += 1) {
    if (i > p) {
      trSmooth = trSmooth - (trSmooth / p) + tr[i]
      plusSmooth = plusSmooth - (plusSmooth / p) + plusDM[i]
      minusSmooth = minusSmooth - (minusSmooth / p) + minusDM[i]
    }

    if (trSmooth <= 0) continue
    const pdi = (plusSmooth / trSmooth) * 100
    const mdi = (minusSmooth / trSmooth) * 100
    plusDI[i] = pdi
    minusDI[i] = mdi
    const den = pdi + mdi
    dx[i] = den === 0 ? 0 : (Math.abs(pdi - mdi) / den) * 100
  }

  let adxSeedCount = 0
  let adxSeed = 0
  for (let i = p; i < n; i += 1) {
    if (dx[i] !== null && adxSeedCount < p) {
      adxSeed += dx[i] as number
      adxSeedCount += 1
      if (adxSeedCount === p) {
        adx[i] = adxSeed / p
      }
      continue
    }

    if (dx[i] !== null) {
      const prev = adx[i - 1]
      if (prev !== null) {
        adx[i] = ((prev * (p - 1)) + (dx[i] as number)) / p
      }
    }
  }

  return { plusDI, minusDI, adx }
}

function stochasticValues(highs: number[], lows: number[], closes: number[], kPeriod: number, dPeriod: number) {
  const kP = clampPositiveInt(kPeriod, 14)
  const dP = clampPositiveInt(dPeriod, 3)
  const n = closes.length
  const kVals: Array<number | null> = Array(n).fill(null)
  const dVals: Array<number | null> = Array(n).fill(null)
  if (n < kP) return { kVals, dVals }

  for (let i = kP - 1; i < n; i += 1) {
    const h = Math.max(...highs.slice(i - kP + 1, i + 1))
    const l = Math.min(...lows.slice(i - kP + 1, i + 1))
    const den = h - l
    kVals[i] = den === 0 ? 50 : ((closes[i] - l) / den) * 100
  }

  for (let i = n - 1; i >= 0; i -= 1) {
    if (kVals[i] === null) continue
    if (i < dP - 1) continue
    const window = kVals.slice(i - dP + 1, i + 1).filter((v): v is number => v !== null)
    if (window.length === dP) {
      dVals[i] = window.reduce((acc, v) => acc + v, 0) / dP
    }
  }

  return { kVals, dVals }
}

function trixValues(closes: number[], period: number, signalPeriod: number) {
  const p = clampPositiveInt(period, 9)
  const s = clampPositiveInt(signalPeriod, 9)
  const ema1 = emaValues(closes, p)
  const ema2 = emaValues(ema1.map((v) => v ?? 0), p)
  const ema3 = emaValues(ema2.map((v) => v ?? 0), p)
  const trix: Array<number | null> = Array(closes.length).fill(null)

  for (let i = 1; i < closes.length; i += 1) {
    const cur = ema3[i]
    const prev = ema3[i - 1]
    if (cur === null || prev === null || prev === 0) continue
    trix[i] = ((cur - prev) / prev) * 100
  }

  const trixOnly = trix.map((v) => v ?? 0)
  const signal = emaValues(trixOnly, s)
  return { trix, signal }
}

type IndicatorPreset = 'scalp' | 'swing' | 'position'
export default function ChartsPage() {
  const [market, setMarket] = useState('FOREX')
  const [symbol, setSymbol] = useState('')
  const [timeframe, setTimeframe] = useState('1h')
  const [liveCandles, setLiveCandles] = useState<OHLCVCandle[]>([])
  const [showSMA, setShowSMA] = useState(true)
  const [smaPeriod, setSmaPeriod] = useState(20)
  const [showEMA, setShowEMA] = useState(false)
  const [emaPeriod, setEmaPeriod] = useState(9)
  const [showDidi, setShowDidi] = useState(false)
  const [didiFastPeriod, setDidiFastPeriod] = useState(3)
  const [didiMidPeriod, setDidiMidPeriod] = useState(8)
  const [didiSlowPeriod, setDidiSlowPeriod] = useState(20)
  const [showBollinger, setShowBollinger] = useState(false)
  const [bollingerPeriod, setBollingerPeriod] = useState(20)
  const [bollingerStdDev, setBollingerStdDev] = useState(2)
  const [showDMI, setShowDMI] = useState(false)
  const [dmiPeriod, setDmiPeriod] = useState(14)
  const [showStochastic, setShowStochastic] = useState(false)
  const [stochasticKPeriod, setStochasticKPeriod] = useState(14)
  const [stochasticDPeriod, setStochasticDPeriod] = useState(3)
  const [showTRIX, setShowTRIX] = useState(false)
  const [trixPeriod, setTrixPeriod] = useState(9)
  const [trixSignalPeriod, setTrixSignalPeriod] = useState(9)
  const [customPresets, setCustomPresets] = useState<CustomIndicatorPreset[]>([])
  const [presetName, setPresetName] = useState('')
  const [selectedCustomPresetId, setSelectedCustomPresetId] = useState('')
  const [importFeedback, setImportFeedback] = useState('')
  const [syncedCrosshairTime, setSyncedCrosshairTime] = useState<number | null>(null)
  const importFileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    setSyncedCrosshairTime(null)
  }, [symbol, timeframe, market])

  function getCurrentIndicatorSettings(): IndicatorSettings {
    return {
      showSMA,
      smaPeriod,
      showEMA,
      emaPeriod,
      showDidi,
      didiFastPeriod,
      didiMidPeriod,
      didiSlowPeriod,
      showBollinger,
      bollingerPeriod,
      bollingerStdDev,
      showDMI,
      dmiPeriod,
      showStochastic,
      stochasticKPeriod,
      stochasticDPeriod,
      showTRIX,
      trixPeriod,
      trixSignalPeriod,
    }
  }

  function applyIndicatorSettings(settings: IndicatorSettings) {
    setShowSMA(settings.showSMA)
    setSmaPeriod(clampPositiveInt(settings.smaPeriod, DEFAULT_INDICATOR_SETTINGS.smaPeriod))
    setShowEMA(settings.showEMA)
    setEmaPeriod(clampPositiveInt(settings.emaPeriod, DEFAULT_INDICATOR_SETTINGS.emaPeriod))
    setShowDidi(settings.showDidi)
    setDidiFastPeriod(clampPositiveInt(settings.didiFastPeriod, DEFAULT_INDICATOR_SETTINGS.didiFastPeriod))
    setDidiMidPeriod(clampPositiveInt(settings.didiMidPeriod, DEFAULT_INDICATOR_SETTINGS.didiMidPeriod))
    setDidiSlowPeriod(clampPositiveInt(settings.didiSlowPeriod, DEFAULT_INDICATOR_SETTINGS.didiSlowPeriod))
    setShowBollinger(settings.showBollinger)
    setBollingerPeriod(clampPositiveInt(settings.bollingerPeriod, DEFAULT_INDICATOR_SETTINGS.bollingerPeriod))
    setBollingerStdDev(settings.bollingerStdDev > 0 ? settings.bollingerStdDev : DEFAULT_INDICATOR_SETTINGS.bollingerStdDev)
    setShowDMI(settings.showDMI)
    setDmiPeriod(clampPositiveInt(settings.dmiPeriod, DEFAULT_INDICATOR_SETTINGS.dmiPeriod))
    setShowStochastic(settings.showStochastic)
    setStochasticKPeriod(clampPositiveInt(settings.stochasticKPeriod, DEFAULT_INDICATOR_SETTINGS.stochasticKPeriod))
    setStochasticDPeriod(clampPositiveInt(settings.stochasticDPeriod, DEFAULT_INDICATOR_SETTINGS.stochasticDPeriod))
    setShowTRIX(settings.showTRIX)
    setTrixPeriod(clampPositiveInt(settings.trixPeriod, DEFAULT_INDICATOR_SETTINGS.trixPeriod))
    setTrixSignalPeriod(clampPositiveInt(settings.trixSignalPeriod, DEFAULT_INDICATOR_SETTINGS.trixSignalPeriod))
  }

  function resetIndicatorSettings() {
    applyIndicatorSettings(DEFAULT_INDICATOR_SETTINGS)
  }

  // Símbolos disponíveis (parquets locais)
  const { data: symbolMapData } = useQuery({
    queryKey: ['market-symbols'],
    queryFn: () => api.getAvailableSymbols(),
    staleTime: 60_000,
  })
  const symbolMap = symbolMapData ?? EMPTY_SYMBOL_MAP

  const marketSymbols = symbolMap[market] ?? []

  // Inicializa símbolo quando o mercado muda
  useEffect(() => {
    if (marketSymbols.length > 0 && !marketSymbols.includes(symbol)) {
      setSymbol(marketSymbols[0])
    }
  }, [market, marketSymbols, symbol])

  // Preços em tempo real
  const { data: prices = [] } = useQuery({
    queryKey: ['market-prices'],
    queryFn: () => api.getPrices(),
    refetchInterval: 5_000,
  })

  // Dados OHLCV do símbolo selecionado
  const { data: candlesData, isFetching: loadingCandles, error: candlesError } = useQuery({
    queryKey: ['ohlcv', symbol, timeframe, market],
    queryFn: () => api.getOHLCV(symbol, timeframe, market, LIMITS[timeframe] ?? 500),
    enabled: !!symbol,
    staleTime: 30_000,
    refetchInterval: 15_000,
  })
  const candles = candlesData ?? EMPTY_CANDLES

  // Evita reaproveitar visual antigo ao trocar símbolo/timeframe/mercado
  useEffect(() => {
    setLiveCandles([])
  }, [symbol, timeframe, market])

  // Quando chega snapshot novo do backend, reseta base local do gráfico.
  useEffect(() => {
    setLiveCandles(candles)
  }, [candles, symbol, timeframe, market])

  useEffect(() => {
    try {
      const raw = localStorage.getItem(CUSTOM_PRESETS_KEY)
      if (!raw) return
      const saved = JSON.parse(raw) as CustomIndicatorPreset[]
      if (!Array.isArray(saved)) return
      setCustomPresets(saved)
    } catch {
      // Ignora presets corrompidos.
    }
  }, [])

  useEffect(() => {
    if (!symbol) return
    const key = `chart:indicators:${market}:${symbol}:${timeframe}`
    try {
      const raw = localStorage.getItem(key)
      if (!raw) {
        applyIndicatorSettings(DEFAULT_INDICATOR_SETTINGS)
        return
      }
      const saved = JSON.parse(raw) as {
        showSMA?: boolean
        smaPeriod?: number
        showEMA?: boolean
        emaPeriod?: number
        showDidi?: boolean
        didiFastPeriod?: number
        didiMidPeriod?: number
        didiSlowPeriod?: number
        showBollinger?: boolean
        bollingerPeriod?: number
        bollingerStdDev?: number
        showDMI?: boolean
        dmiPeriod?: number
        showStochastic?: boolean
        stochasticKPeriod?: number
        stochasticDPeriod?: number
        showTRIX?: boolean
        trixPeriod?: number
        trixSignalPeriod?: number
      }

      applyIndicatorSettings({
        showSMA: typeof saved.showSMA === 'boolean' ? saved.showSMA : DEFAULT_INDICATOR_SETTINGS.showSMA,
        smaPeriod: typeof saved.smaPeriod === 'number' ? saved.smaPeriod : DEFAULT_INDICATOR_SETTINGS.smaPeriod,
        showEMA: typeof saved.showEMA === 'boolean' ? saved.showEMA : DEFAULT_INDICATOR_SETTINGS.showEMA,
        emaPeriod: typeof saved.emaPeriod === 'number' ? saved.emaPeriod : DEFAULT_INDICATOR_SETTINGS.emaPeriod,
        showDidi: typeof saved.showDidi === 'boolean' ? saved.showDidi : DEFAULT_INDICATOR_SETTINGS.showDidi,
        didiFastPeriod: typeof saved.didiFastPeriod === 'number' ? saved.didiFastPeriod : DEFAULT_INDICATOR_SETTINGS.didiFastPeriod,
        didiMidPeriod: typeof saved.didiMidPeriod === 'number' ? saved.didiMidPeriod : DEFAULT_INDICATOR_SETTINGS.didiMidPeriod,
        didiSlowPeriod: typeof saved.didiSlowPeriod === 'number' ? saved.didiSlowPeriod : DEFAULT_INDICATOR_SETTINGS.didiSlowPeriod,
        showBollinger: typeof saved.showBollinger === 'boolean' ? saved.showBollinger : DEFAULT_INDICATOR_SETTINGS.showBollinger,
        bollingerPeriod: typeof saved.bollingerPeriod === 'number' ? saved.bollingerPeriod : DEFAULT_INDICATOR_SETTINGS.bollingerPeriod,
        bollingerStdDev: typeof saved.bollingerStdDev === 'number' ? saved.bollingerStdDev : DEFAULT_INDICATOR_SETTINGS.bollingerStdDev,
        showDMI: typeof saved.showDMI === 'boolean' ? saved.showDMI : DEFAULT_INDICATOR_SETTINGS.showDMI,
        dmiPeriod: typeof saved.dmiPeriod === 'number' ? saved.dmiPeriod : DEFAULT_INDICATOR_SETTINGS.dmiPeriod,
        showStochastic: typeof saved.showStochastic === 'boolean' ? saved.showStochastic : DEFAULT_INDICATOR_SETTINGS.showStochastic,
        stochasticKPeriod: typeof saved.stochasticKPeriod === 'number' ? saved.stochasticKPeriod : DEFAULT_INDICATOR_SETTINGS.stochasticKPeriod,
        stochasticDPeriod: typeof saved.stochasticDPeriod === 'number' ? saved.stochasticDPeriod : DEFAULT_INDICATOR_SETTINGS.stochasticDPeriod,
        showTRIX: typeof saved.showTRIX === 'boolean' ? saved.showTRIX : DEFAULT_INDICATOR_SETTINGS.showTRIX,
        trixPeriod: typeof saved.trixPeriod === 'number' ? saved.trixPeriod : DEFAULT_INDICATOR_SETTINGS.trixPeriod,
        trixSignalPeriod: typeof saved.trixSignalPeriod === 'number' ? saved.trixSignalPeriod : DEFAULT_INDICATOR_SETTINGS.trixSignalPeriod,
      })
    } catch {
      // Ignora JSON inválido salvo anteriormente.
      applyIndicatorSettings(DEFAULT_INDICATOR_SETTINGS)
    }
  }, [market, symbol, timeframe])

  useEffect(() => {
    if (!symbol) return
    const key = `chart:indicators:${market}:${symbol}:${timeframe}`
    const payload = {
      showSMA,
      smaPeriod,
      showEMA,
      emaPeriod,
      showDidi,
      didiFastPeriod,
      didiMidPeriod,
      didiSlowPeriod,
      showBollinger,
      bollingerPeriod,
      bollingerStdDev,
      showDMI,
      dmiPeriod,
      showStochastic,
      stochasticKPeriod,
      stochasticDPeriod,
      showTRIX,
      trixPeriod,
      trixSignalPeriod,
    }
    localStorage.setItem(key, JSON.stringify(payload))
  }, [
    market,
    symbol,
    timeframe,
    showSMA,
    smaPeriod,
    showEMA,
    emaPeriod,
    showDidi,
    didiFastPeriod,
    didiMidPeriod,
    didiSlowPeriod,
    showBollinger,
    bollingerPeriod,
    bollingerStdDev,
    showDMI,
    dmiPeriod,
    showStochastic,
    stochasticKPeriod,
    stochasticDPeriod,
    showTRIX,
    trixPeriod,
    trixSignalPeriod,
  ])

  const activeAuxPaneCount = [showDidi, showDMI, showStochastic, showTRIX].filter(Boolean).length
  const hasAuxiliaryCharts = activeAuxPaneCount > 0

  const secondaryPaneHeight = useMemo(() => {
    if (activeAuxPaneCount >= 4) return 150
    if (activeAuxPaneCount === 3) return 170
    if (activeAuxPaneCount === 2) return 200
    if (activeAuxPaneCount === 1) return 240
    return 0
  }, [activeAuxPaneCount])

  const secondaryPaneHeights = useMemo(
    () => Array.from({ length: activeAuxPaneCount }, () => secondaryPaneHeight),
    [activeAuxPaneCount, secondaryPaneHeight]
  )

  const secondaryChartHeight = useMemo(
    () => (hasAuxiliaryCharts ? activeAuxPaneCount * secondaryPaneHeight : 0),
    [hasAuxiliaryCharts, activeAuxPaneCount, secondaryPaneHeight]
  )

  const mainChartHeight = useMemo(
    () => (hasAuxiliaryCharts ? secondaryChartHeight : 500),
    [hasAuxiliaryCharts, secondaryChartHeight]
  )

  // Trades para markers no gráfico (todos os trades do símbolo)
  const { data: tradeData } = useQuery({
    queryKey: ['trades-history', symbol],
    queryFn: () => api.getTradeHistory({ market, per_page: 200 }),
    enabled: !!symbol,
    staleTime: 30_000,
  })

  const trades: TradeSummary[] = (tradeData?.trades ?? []).filter(
    (t) => t.symbol === symbol
  )

  // Preço atual do símbolo
  const priceInfo = prices.find((p) => p.symbol === symbol)
  const isLiveFeed = prices.some((p) => p.source === 'live')

  // Atualização em tempo real do último candle com base no preço tick (mid).
  useEffect(() => {
    if (!priceInfo || liveCandles.length === 0) return

    const now = Math.floor(Date.now() / 1000)
    const nowBucket = alignToTimeframe(now, timeframe)

    setLiveCandles((prev) => {
      if (prev.length === 0) return prev

      const next = [...prev]
      const last = next[next.length - 1]
      const tick = priceInfo.mid

      if (last.time === nowBucket) {
        next[next.length - 1] = {
          ...last,
          close: tick,
          high: Math.max(last.high, tick),
          low: Math.min(last.low, tick),
        }
      } else if (last.time < nowBucket) {
        next.push({
          time: nowBucket,
          open: last.close,
          high: Math.max(last.close, tick),
          low: Math.min(last.close, tick),
          close: tick,
          volume: 0,
        })
        if (next.length > (LIMITS[timeframe] ?? 500)) {
          next.shift()
        }
      }

      return next
    })
  }, [priceInfo, timeframe, liveCandles.length])

  const lastCandle = liveCandles[liveCandles.length - 1]
  const prevCandle = liveCandles[liveCandles.length - 2]
  const priceDiff = lastCandle && prevCandle
    ? ((lastCandle.close - prevCandle.close) / prevCandle.close) * 100
    : 0

  const markets = Object.keys(symbolMap).length > 0
    ? Object.keys(symbolMap)
    : ['FOREX', 'CRIPTO', 'B3']

  function applyPreset(preset: IndicatorPreset) {
    if (preset === 'scalp') {
      setShowSMA(true)
      setSmaPeriod(9)
      setShowEMA(true)
      setEmaPeriod(21)
      setShowDidi(true)
      setDidiFastPeriod(3)
      setDidiMidPeriod(8)
      setDidiSlowPeriod(20)
      setShowBollinger(true)
      setBollingerPeriod(20)
      setBollingerStdDev(2)
      setShowDMI(true)
      setDmiPeriod(14)
      setShowStochastic(true)
      setStochasticKPeriod(14)
      setStochasticDPeriod(3)
      setShowTRIX(true)
      setTrixPeriod(9)
      setTrixSignalPeriod(9)
      return
    }

    if (preset === 'swing') {
      setShowSMA(true)
      setSmaPeriod(20)
      setShowEMA(true)
      setEmaPeriod(50)
      setShowDidi(true)
      setDidiFastPeriod(5)
      setDidiMidPeriod(13)
      setDidiSlowPeriod(34)
      setShowBollinger(true)
      setBollingerPeriod(20)
      setBollingerStdDev(2)
      setShowDMI(true)
      setDmiPeriod(14)
      setShowStochastic(true)
      setStochasticKPeriod(14)
      setStochasticDPeriod(3)
      setShowTRIX(true)
      setTrixPeriod(15)
      setTrixSignalPeriod(9)
      return
    }

    setShowSMA(true)
    setSmaPeriod(50)
    setShowEMA(true)
    setEmaPeriod(200)
    setShowDidi(true)
    setDidiFastPeriod(8)
    setDidiMidPeriod(20)
    setDidiSlowPeriod(55)
    setShowBollinger(true)
    setBollingerPeriod(20)
    setBollingerStdDev(2)
    setShowDMI(true)
    setDmiPeriod(14)
    setShowStochastic(false)
    setShowTRIX(true)
    setTrixPeriod(18)
    setTrixSignalPeriod(9)
  }

  function saveCustomPreset() {
    const normalizedName = (presetName.trim() || `${symbol} ${timeframe}`).slice(0, 60)
    const id = normalizedName.toLowerCase().replace(/\s+/g, '-')
    const now = new Date().toISOString()
    const newPreset: CustomIndicatorPreset = {
      id,
      name: normalizedName,
      settings: getCurrentIndicatorSettings(),
      updatedAt: now,
    }

    const next = [
      newPreset,
      ...customPresets.filter((p) => p.id !== id),
    ].slice(0, 30)

    setCustomPresets(next)
    setSelectedCustomPresetId(id)
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(next))
  }

  function applySelectedCustomPreset() {
    if (!selectedCustomPresetId) return
    const preset = customPresets.find((p) => p.id === selectedCustomPresetId)
    if (!preset) return
    applyIndicatorSettings(preset.settings)
  }

  function deleteSelectedCustomPreset() {
    if (!selectedCustomPresetId) return
    const next = customPresets.filter((p) => p.id !== selectedCustomPresetId)
    setCustomPresets(next)
    setSelectedCustomPresetId('')
    localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(next))
  }

  function isValidIndicatorSettings(value: unknown): value is IndicatorSettings {
    if (!value || typeof value !== 'object') return false
    const v = value as Partial<IndicatorSettings>
    return (
      typeof v.showSMA === 'boolean' &&
      typeof v.smaPeriod === 'number' &&
      typeof v.showEMA === 'boolean' &&
      typeof v.emaPeriod === 'number' &&
      typeof v.showDidi === 'boolean' &&
      typeof v.didiFastPeriod === 'number' &&
      typeof v.didiMidPeriod === 'number' &&
      typeof v.didiSlowPeriod === 'number' &&
      typeof v.showBollinger === 'boolean' &&
      typeof v.bollingerPeriod === 'number' &&
      typeof v.bollingerStdDev === 'number' &&
      typeof v.showDMI === 'boolean' &&
      typeof v.dmiPeriod === 'number' &&
      typeof v.showStochastic === 'boolean' &&
      typeof v.stochasticKPeriod === 'number' &&
      typeof v.stochasticDPeriod === 'number' &&
      typeof v.showTRIX === 'boolean' &&
      typeof v.trixPeriod === 'number' &&
      typeof v.trixSignalPeriod === 'number'
    )
  }

  function exportCustomPresets() {
    const payload: PresetsExportPayload = {
      version: 1,
      exportedAt: new Date().toISOString(),
      presets: customPresets,
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `autoxtrade-indicator-presets-${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  function downloadPresetTemplate() {
    const template: PresetsExportPayload = {
      version: 1,
      exportedAt: new Date().toISOString(),
      presets: [
        {
          id: 'meu-preset-exemplo',
          name: 'Meu Preset Exemplo',
          updatedAt: new Date().toISOString(),
          settings: {
            showSMA: true,
            smaPeriod: 20,
            showEMA: true,
            emaPeriod: 50,
            showDidi: true,
            didiFastPeriod: 3,
            didiMidPeriod: 8,
            didiSlowPeriod: 20,
            showBollinger: true,
            bollingerPeriod: 20,
            bollingerStdDev: 2,
            showDMI: true,
            dmiPeriod: 14,
            showStochastic: true,
            stochasticKPeriod: 14,
            stochasticDPeriod: 3,
            showTRIX: true,
            trixPeriod: 9,
            trixSignalPeriod: 9,
          },
        },
      ],
    }

    const blob = new Blob([JSON.stringify(template, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'autoxtrade-indicator-presets-template.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  function openImportDialog() {
    importFileRef.current?.click()
  }

  async function importCustomPresetsFromFile(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return

    try {
      const text = await file.text()
      const parsed = JSON.parse(text) as Partial<PresetsExportPayload> | CustomIndicatorPreset[]
      const importedRaw = Array.isArray(parsed)
        ? parsed
        : Array.isArray(parsed.presets)
          ? parsed.presets
          : []

      const normalized = importedRaw
        .filter((p): p is CustomIndicatorPreset => {
          if (!p || typeof p !== 'object') return false
          if (typeof p.id !== 'string' || typeof p.name !== 'string' || typeof p.updatedAt !== 'string') return false
          return isValidIndicatorSettings(p.settings)
        })
        .map((p) => ({
          id: p.id.trim() || p.name.toLowerCase().replace(/\s+/g, '-'),
          name: p.name.trim().slice(0, 60) || 'Preset',
          updatedAt: p.updatedAt,
          settings: {
            showSMA: p.settings.showSMA,
            smaPeriod: clampPositiveInt(p.settings.smaPeriod, DEFAULT_INDICATOR_SETTINGS.smaPeriod),
            showEMA: p.settings.showEMA,
            emaPeriod: clampPositiveInt(p.settings.emaPeriod, DEFAULT_INDICATOR_SETTINGS.emaPeriod),
            showDidi: p.settings.showDidi,
            didiFastPeriod: clampPositiveInt(p.settings.didiFastPeriod, DEFAULT_INDICATOR_SETTINGS.didiFastPeriod),
            didiMidPeriod: clampPositiveInt(p.settings.didiMidPeriod, DEFAULT_INDICATOR_SETTINGS.didiMidPeriod),
            didiSlowPeriod: clampPositiveInt(p.settings.didiSlowPeriod, DEFAULT_INDICATOR_SETTINGS.didiSlowPeriod),
            showBollinger: p.settings.showBollinger,
            bollingerPeriod: clampPositiveInt(p.settings.bollingerPeriod, DEFAULT_INDICATOR_SETTINGS.bollingerPeriod),
            bollingerStdDev: p.settings.bollingerStdDev > 0 ? p.settings.bollingerStdDev : DEFAULT_INDICATOR_SETTINGS.bollingerStdDev,
            showDMI: p.settings.showDMI,
            dmiPeriod: clampPositiveInt(p.settings.dmiPeriod, DEFAULT_INDICATOR_SETTINGS.dmiPeriod),
            showStochastic: p.settings.showStochastic,
            stochasticKPeriod: clampPositiveInt(p.settings.stochasticKPeriod, DEFAULT_INDICATOR_SETTINGS.stochasticKPeriod),
            stochasticDPeriod: clampPositiveInt(p.settings.stochasticDPeriod, DEFAULT_INDICATOR_SETTINGS.stochasticDPeriod),
            showTRIX: p.settings.showTRIX,
            trixPeriod: clampPositiveInt(p.settings.trixPeriod, DEFAULT_INDICATOR_SETTINGS.trixPeriod),
            trixSignalPeriod: clampPositiveInt(p.settings.trixSignalPeriod, DEFAULT_INDICATOR_SETTINGS.trixSignalPeriod),
          },
        }))

      if (normalized.length === 0) {
        setImportFeedback('Nenhum preset válido encontrado no arquivo.')
        return
      }

      const merged = [...normalized, ...customPresets]
      const dedupById = new Map<string, CustomIndicatorPreset>()
      for (const preset of merged) {
        dedupById.set(preset.id, preset)
      }
      const next = Array.from(dedupById.values())
        .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
        .slice(0, 100)

      setCustomPresets(next)
      localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(next))
      setImportFeedback(`Importados ${normalized.length} preset(s) com sucesso.`)
    } catch {
      setImportFeedback('Arquivo inválido. Verifique se é um JSON de presets.')
    }
  }

  const overlayLines = useMemo<OverlayLine[]>(() => {
    if (liveCandles.length === 0) return []

    const lines: OverlayLine[] = []
    const closes = liveCandles.map((c) => c.close)
    const highs = liveCandles.map((c) => c.high)
    const lows = liveCandles.map((c) => c.low)
    let nextPaneIndex = 1
    const didiPaneIndex = showDidi ? nextPaneIndex++ : undefined
    const dmiPaneIndex = showDMI ? nextPaneIndex++ : undefined
    const stochasticPaneIndex = showStochastic ? nextPaneIndex++ : undefined
    const trixPaneIndex = showTRIX ? nextPaneIndex++ : undefined

    if (showSMA) {
      const period = clampPositiveInt(smaPeriod, 20)
      const sma = smaValues(closes, period)
      lines.push({
        id: `sma-${period}`,
        label: `SMA ${period}`,
        color: '#f59e0b',
        priceScaleId: 'right',
        paneIndex: 0,
        data: liveCandles
          .map((c, idx) => ({ time: c.time, value: sma[idx] }))
          .filter((p): p is { time: number; value: number } => p.value !== null),
      })
    }

    if (showEMA) {
      const period = clampPositiveInt(emaPeriod, 9)
      const ema = emaValues(closes, period)
      lines.push({
        id: `ema-${period}`,
        label: `EMA ${period}`,
        color: '#a78bfa',
        priceScaleId: 'right',
        paneIndex: 0,
        data: liveCandles
          .map((c, idx) => ({ time: c.time, value: ema[idx] }))
          .filter((p): p is { time: number; value: number } => p.value !== null),
      })
    }

    if (showBollinger) {
      const p = clampPositiveInt(bollingerPeriod, 20)
      const m = bollingerStdDev > 0 ? bollingerStdDev : 2
      const bb = bollingerValues(closes, p, m)
      lines.push(
        {
          id: `bb-mid-${p}-${m}`,
          label: `Bollinger Middle ${p}`,
          color: '#60a5fa',
          priceScaleId: 'right',
          paneIndex: 0,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: bb[idx].mid }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        },
        {
          id: `bb-up-${p}-${m}`,
          label: `Bollinger Upper ${m}`,
          color: '#22c55e',
          priceScaleId: 'right',
          paneIndex: 0,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: bb[idx].upper }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        },
        {
          id: `bb-low-${p}-${m}`,
          label: `Bollinger Lower ${m}`,
          color: '#ef4444',
          priceScaleId: 'right',
          paneIndex: 0,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: bb[idx].lower }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        }
      )
    }

    if (showDidi) {
      const fast = clampPositiveInt(didiFastPeriod, 3)
      const mid = clampPositiveInt(didiMidPeriod, 8)
      const slow = clampPositiveInt(didiSlowPeriod, 20)

      const maFast = smaValues(closes, fast)
      const maMid = smaValues(closes, mid)
      const maSlow = smaValues(closes, slow)

      const didiFast = liveCandles
        .map((c, idx) => {
          const f = maFast[idx]
          const m = maMid[idx]
          if (f === null || m === null || m === 0) return null
          return { time: c.time, value: ((f / m) - 1) * 100 }
        })
        .filter((p): p is { time: number; value: number } => p !== null)

      const didiSlow = liveCandles
        .map((c, idx) => {
          const s = maSlow[idx]
          const m = maMid[idx]
          if (s === null || m === null || m === 0) return null
          return { time: c.time, value: ((s / m) - 1) * 100 }
        })
        .filter((p): p is { time: number; value: number } => p !== null)

      const didiZero = liveCandles.map((c) => ({ time: c.time, value: 0 }))

      lines.push(
        {
          id: `didi-fast-${fast}-${mid}`,
          label: 'Didi Rápida',
          color: '#22d3ee',
          priceScaleId: 'left',
          paneIndex: didiPaneIndex,
          data: didiFast,
        },
        {
          id: `didi-slow-${slow}-${mid}`,
          label: 'Didi Lenta',
          color: '#f43f5e',
          priceScaleId: 'left',
          paneIndex: didiPaneIndex,
          data: didiSlow,
        },
        {
          id: 'didi-zero',
          label: 'Didi Zero',
          color: '#64748b',
          priceScaleId: 'left',
          paneIndex: didiPaneIndex,
          data: didiZero,
        }
      )
    }

    if (showDMI) {
      const dmi = dmiValues(highs, lows, closes, dmiPeriod)
      lines.push(
        {
          id: `dmi-plus-${dmiPeriod}`,
          label: `+DI ${dmiPeriod}`,
          color: '#22c55e',
          priceScaleId: 'left',
          paneIndex: dmiPaneIndex,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: dmi.plusDI[idx] }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        },
        {
          id: `dmi-minus-${dmiPeriod}`,
          label: `-DI ${dmiPeriod}`,
          color: '#ef4444',
          priceScaleId: 'left',
          paneIndex: dmiPaneIndex,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: dmi.minusDI[idx] }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        },
        {
          id: `adx-${dmiPeriod}`,
          label: `ADX ${dmiPeriod}`,
          color: '#f59e0b',
          priceScaleId: 'left',
          paneIndex: dmiPaneIndex,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: dmi.adx[idx] }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        }
      )
    }

    if (showStochastic) {
      const st = stochasticValues(highs, lows, closes, stochasticKPeriod, stochasticDPeriod)
      lines.push(
        {
          id: `stoch-k-${stochasticKPeriod}`,
          label: `%K ${stochasticKPeriod}`,
          color: '#38bdf8',
          priceScaleId: 'left',
          paneIndex: stochasticPaneIndex,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: st.kVals[idx] }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        },
        {
          id: `stoch-d-${stochasticDPeriod}`,
          label: `%D ${stochasticDPeriod}`,
          color: '#f472b6',
          priceScaleId: 'left',
          paneIndex: stochasticPaneIndex,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: st.dVals[idx] }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        }
      )
    }

    if (showTRIX) {
      const tx = trixValues(closes, trixPeriod, trixSignalPeriod)
      lines.push(
        {
          id: `trix-${trixPeriod}`,
          label: `TRIX ${trixPeriod}`,
          color: '#14b8a6',
          priceScaleId: 'left',
          paneIndex: trixPaneIndex,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: tx.trix[idx] }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        },
        {
          id: `trix-sig-${trixSignalPeriod}`,
          label: `TRIX Signal ${trixSignalPeriod}`,
          color: '#fb923c',
          priceScaleId: 'left',
          paneIndex: trixPaneIndex,
          data: liveCandles
            .map((c, idx) => ({ time: c.time, value: tx.signal[idx] }))
            .filter((x): x is { time: number; value: number } => x.value !== null),
        }
      )
    }

    return lines
  }, [
    liveCandles,
    showSMA,
    smaPeriod,
    showEMA,
    emaPeriod,
    showDidi,
    didiFastPeriod,
    didiMidPeriod,
    didiSlowPeriod,
    showBollinger,
    bollingerPeriod,
    bollingerStdDev,
    showDMI,
    dmiPeriod,
    showStochastic,
    stochasticKPeriod,
    stochasticDPeriod,
    showTRIX,
    trixPeriod,
    trixSignalPeriod,
  ])

  const paneLegends = useMemo<PaneLegend[]>(() => {
    const panes: PaneLegend[] = [
      {
        paneIndex: 0,
        title: 'Principal',
        lines: overlayLines.filter((line) => (line.paneIndex ?? 0) === 0).map((line) => ({
          label: line.label,
          color: line.color,
        })),
      },
    ]

    let nextPaneIndex = 1
    if (showDidi) {
      panes.push({
        paneIndex: nextPaneIndex,
        title: 'Didi',
        lines: overlayLines.filter((line) => line.paneIndex === nextPaneIndex).map((line) => ({
          label: line.label,
          color: line.color,
        })),
      })
      nextPaneIndex += 1
    }

    if (showDMI) {
      panes.push({
        paneIndex: nextPaneIndex,
        title: 'DMI / ADX',
        lines: overlayLines.filter((line) => line.paneIndex === nextPaneIndex).map((line) => ({
          label: line.label,
          color: line.color,
        })),
      })
      nextPaneIndex += 1
    }

    if (showStochastic) {
      panes.push({
        paneIndex: nextPaneIndex,
        title: 'Estocástico',
        lines: overlayLines.filter((line) => line.paneIndex === nextPaneIndex).map((line) => ({
          label: line.label,
          color: line.color,
        })),
      })
      nextPaneIndex += 1
    }

    if (showTRIX) {
      panes.push({
        paneIndex: nextPaneIndex,
        title: 'TRIX',
        lines: overlayLines.filter((line) => line.paneIndex === nextPaneIndex).map((line) => ({
          label: line.label,
          color: line.color,
        })),
      })
    }

    return panes.filter((pane) => pane.lines.length > 0)
  }, [overlayLines, showDidi, showDMI, showStochastic, showTRIX])

  const mainPaneLegend = useMemo(
    () => paneLegends.find((pane) => pane.paneIndex === 0),
    [paneLegends]
  )

  const auxiliaryPaneLegends = useMemo(
    () => paneLegends.filter((pane) => pane.paneIndex > 0),
    [paneLegends]
  )

  const mainOverlayLines = useMemo(
    () => overlayLines
      .filter((line) => (line.paneIndex ?? 0) === 0)
      .map((line) => ({ ...line, paneIndex: 0 })),
    [overlayLines]
  )

  const auxiliaryOverlayLines = useMemo(
    () => overlayLines
      .filter((line) => (line.paneIndex ?? 0) > 0)
      .map((line) => ({
        ...line,
        paneIndex: Math.max(0, (line.paneIndex ?? 1) - 1),
      })),
    [overlayLines]
  )

  const auxiliaryCharts = useMemo(
    () => auxiliaryPaneLegends.map((pane, idx) => ({
      key: `${pane.paneIndex}:${pane.title}:${idx}`,
      title: pane.title,
      overlayLines: auxiliaryOverlayLines
        .filter((line) => (line.paneIndex ?? 0) === idx)
        .map((line) => ({ ...line, paneIndex: 0 })),
    })),
    [auxiliaryPaneLegends, auxiliaryOverlayLines]
  )

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-white">Gráficos</h1>
          <p className="text-sm text-slate-400">Candlestick com marcadores de trades</p>
        </div>

        {/* Controles */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Mercado */}
          <div className="flex rounded-lg overflow-hidden border border-slate-700">
            {markets.map((m) => (
              <button
                key={m}
                onClick={() => setMarket(m)}
                className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                  market === m
                    ? 'bg-blue-600 text-white'
                    : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
                }`}
              >
                {m}
              </button>
            ))}
          </div>

          {/* Símbolo */}
          <select
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            className="bg-slate-800 border border-slate-700 text-white text-sm rounded-lg px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            {marketSymbols.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>

          {/* Timeframe */}
          <div className="flex rounded-lg overflow-hidden border border-slate-700">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                className={`px-2.5 py-1.5 text-xs font-medium transition-colors ${
                  timeframe === tf
                    ? 'bg-blue-600 text-white'
                    : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
                }`}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Info bar do símbolo */}
      {symbol && (
        <div className="bg-slate-800/50 border border-slate-700 rounded-lg px-4 py-2 flex flex-wrap items-center gap-6">
          <div>
            <span className="text-lg font-bold text-white">{symbol}</span>
            <span className="ml-2 text-xs text-slate-400">{market} · {timeframe}</span>
          </div>

          {lastCandle && (
            <>
              <div className="flex items-center gap-1">
                <span className="text-xs text-slate-400">Fech.</span>
                <span className="font-mono font-semibold text-white">
                  {fmt(lastCandle.close)}
                </span>
                <span className={`text-xs font-medium ${pctColor(priceDiff)}`}>
                  {priceDiff > 0 ? '+' : ''}{priceDiff.toFixed(3)}%
                </span>
              </div>
              <div className="flex items-center gap-3 text-xs text-slate-400">
                <span>A: <span className="text-white font-mono">{fmt(lastCandle.high)}</span></span>
                <span>B: <span className="text-white font-mono">{fmt(lastCandle.low)}</span></span>
                <span>Vol: <span className="text-white font-mono">{lastCandle.volume.toLocaleString('pt-BR')}</span></span>
              </div>
            </>
          )}

          {priceInfo && (
            <div className="flex items-center gap-3 text-xs ml-auto">
              <span className="text-slate-400">Bid: <span className="text-emerald-400 font-mono">{fmt(priceInfo.bid)}</span></span>
              <span className="text-slate-400">Ask: <span className="text-red-400 font-mono">{fmt(priceInfo.ask)}</span></span>
              <span className="flex items-center gap-1">
                <span className={`w-2 h-2 rounded-full animate-pulse ${isLiveFeed ? 'bg-emerald-400' : 'bg-amber-400'}`} />
                <span className="text-slate-400">{isLiveFeed ? 'Live' : 'Fallback'}</span>
              </span>
            </div>
          )}
        </div>
      )}

      {!isLiveFeed && (
        <div className="rounded-lg border border-amber-700/60 bg-amber-900/20 px-4 py-2 text-amber-200 text-sm">
          Feed MT5 ao vivo indisponível. O gráfico está usando fallback do último Parquet (pode ficar estático).
        </div>
      )}

      {/* Indicadores técnicos */}
      <div className="rounded-lg border border-slate-700 bg-slate-800/40 p-3">
        <div className="flex flex-wrap items-center gap-4">
          <div className="text-sm font-semibold text-slate-200">Indicadores</div>

          <div className="flex items-center gap-2">
            <button
              onClick={() => applyPreset('scalp')}
              className="rounded border border-slate-600 bg-slate-900 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
            >
              Preset Scalp
            </button>
            <button
              onClick={() => applyPreset('swing')}
              className="rounded border border-slate-600 bg-slate-900 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
            >
              Preset Swing
            </button>
            <button
              onClick={() => applyPreset('position')}
              className="rounded border border-slate-600 bg-slate-900 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
            >
              Preset Position
            </button>
            <button
              onClick={resetIndicatorSettings}
              className="rounded border border-amber-700/60 bg-amber-900/20 px-2 py-1 text-xs text-amber-200 hover:bg-amber-900/35"
            >
              Reset Padrão
            </button>
          </div>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={showSMA}
              onChange={(e) => setShowSMA(e.target.checked)}
              className="rounded border-slate-600 bg-slate-800"
            />
            Média Móvel (SMA)
          </label>

          <label className="flex items-center gap-2 text-xs text-slate-400">
            Período
            <input
              type="number"
              min={1}
              max={500}
              value={smaPeriod}
              onChange={(e) => setSmaPeriod(clampPositiveInt(Number(e.target.value), 20))}
              className="w-20 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={showEMA}
              onChange={(e) => setShowEMA(e.target.checked)}
              className="rounded border-slate-600 bg-slate-800"
            />
            Média Exponencial (EMA)
          </label>

          <label className="flex items-center gap-2 text-xs text-slate-400">
            Período EMA
            <input
              type="number"
              min={1}
              max={500}
              value={emaPeriod}
              onChange={(e) => setEmaPeriod(clampPositiveInt(Number(e.target.value), 9))}
              className="w-20 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={showDidi}
              onChange={(e) => setShowDidi(e.target.checked)}
              className="rounded border-slate-600 bg-slate-800"
            />
            Didi Index
          </label>

          <label className="flex items-center gap-2 text-xs text-slate-400">
            Rápida
            <input
              type="number"
              min={1}
              max={200}
              value={didiFastPeriod}
              onChange={(e) => setDidiFastPeriod(clampPositiveInt(Number(e.target.value), 3))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Base
            <input
              type="number"
              min={1}
              max={200}
              value={didiMidPeriod}
              onChange={(e) => setDidiMidPeriod(clampPositiveInt(Number(e.target.value), 8))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Lenta
            <input
              type="number"
              min={1}
              max={300}
              value={didiSlowPeriod}
              onChange={(e) => setDidiSlowPeriod(clampPositiveInt(Number(e.target.value), 20))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={showBollinger}
              onChange={(e) => setShowBollinger(e.target.checked)}
              className="rounded border-slate-600 bg-slate-800"
            />
            Bollinger
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            BB Per.
            <input
              type="number"
              min={1}
              max={400}
              value={bollingerPeriod}
              onChange={(e) => setBollingerPeriod(clampPositiveInt(Number(e.target.value), 20))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            BB Desv.
            <input
              type="number"
              min={0.1}
              step={0.1}
              max={6}
              value={bollingerStdDev}
              onChange={(e) => setBollingerStdDev(Math.max(0.1, Number(e.target.value) || 2))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={showDMI}
              onChange={(e) => setShowDMI(e.target.checked)}
              className="rounded border-slate-600 bg-slate-800"
            />
            DMI / ADX
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            DMI Per.
            <input
              type="number"
              min={2}
              max={100}
              value={dmiPeriod}
              onChange={(e) => setDmiPeriod(clampPositiveInt(Number(e.target.value), 14))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={showStochastic}
              onChange={(e) => setShowStochastic(e.target.checked)}
              className="rounded border-slate-600 bg-slate-800"
            />
            Estocástico
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            %K
            <input
              type="number"
              min={2}
              max={100}
              value={stochasticKPeriod}
              onChange={(e) => setStochasticKPeriod(clampPositiveInt(Number(e.target.value), 14))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            %D
            <input
              type="number"
              min={1}
              max={30}
              value={stochasticDPeriod}
              onChange={(e) => setStochasticDPeriod(clampPositiveInt(Number(e.target.value), 3))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>

          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={showTRIX}
              onChange={(e) => setShowTRIX(e.target.checked)}
              className="rounded border-slate-600 bg-slate-800"
            />
            TRIX
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            TRIX Per.
            <input
              type="number"
              min={2}
              max={80}
              value={trixPeriod}
              onChange={(e) => setTrixPeriod(clampPositiveInt(Number(e.target.value), 9))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            Sinal
            <input
              type="number"
              min={1}
              max={80}
              value={trixSignalPeriod}
              onChange={(e) => setTrixSignalPeriod(clampPositiveInt(Number(e.target.value), 9))}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>

        </div>


        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-700/70 pt-3">
          <input
            type="text"
            value={presetName}
            onChange={(e) => setPresetName(e.target.value)}
            placeholder={symbol ? `Preset ${symbol} ${timeframe}` : 'Nome do preset'}
            className="w-56 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200"
          />
          <button
            onClick={saveCustomPreset}
            disabled={!symbol}
            className="rounded border border-emerald-700/70 bg-emerald-900/20 px-2 py-1 text-xs text-emerald-200 hover:bg-emerald-900/35 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Salvar preset atual
          </button>

          <select
            value={selectedCustomPresetId}
            onChange={(e) => setSelectedCustomPresetId(e.target.value)}
            className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-xs text-slate-200"
          >
            <option value="">Selecionar preset salvo</option>
            {customPresets.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>

          <button
            onClick={applySelectedCustomPreset}
            disabled={!selectedCustomPresetId}
            className="rounded border border-blue-700/70 bg-blue-900/20 px-2 py-1 text-xs text-blue-200 hover:bg-blue-900/35 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Aplicar preset salvo
          </button>
          <button
            onClick={deleteSelectedCustomPreset}
            disabled={!selectedCustomPresetId}
            className="rounded border border-red-700/70 bg-red-900/20 px-2 py-1 text-xs text-red-200 hover:bg-red-900/35 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Excluir preset
          </button>

          <button
            onClick={exportCustomPresets}
            disabled={customPresets.length === 0}
            className="rounded border border-indigo-700/70 bg-indigo-900/20 px-2 py-1 text-xs text-indigo-200 hover:bg-indigo-900/35 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Exportar JSON
          </button>

          <button
            onClick={openImportDialog}
            className="rounded border border-cyan-700/70 bg-cyan-900/20 px-2 py-1 text-xs text-cyan-200 hover:bg-cyan-900/35"
          >
            Importar JSON
          </button>

          <button
            onClick={downloadPresetTemplate}
            className="rounded border border-violet-700/70 bg-violet-900/20 px-2 py-1 text-xs text-violet-200 hover:bg-violet-900/35"
          >
            Baixar template JSON
          </button>

          <input
            ref={importFileRef}
            type="file"
            accept="application/json,.json"
            onChange={importCustomPresetsFromFile}
            className="hidden"
          />
        </div>

        {importFeedback && (
          <div className="mt-2 text-xs text-slate-400">{importFeedback}</div>
        )}

        <div className="mt-2 text-xs text-slate-500">
          Principal: candles + MM + Bollinger. Auxiliares: Didi, DMI/ADX, Estocástico e TRIX em painéis separados. Configuração salva por ativo/timeframe.
        </div>

        {paneLegends.length > 0 && (
          <div className="mt-2 text-[11px] text-slate-500">
            Legenda por painel disponível na lateral de cada bloco.
          </div>
        )}
      </div>

      {/* Gráfico principal */}
      {!symbol ? (
        <div className="w-full rounded-lg border border-slate-700 bg-slate-800/30 flex flex-col items-center justify-center gap-2" style={{ height: mainChartHeight }}>
          <p className="text-slate-400">Nenhum dado disponível.</p>
          <p className="text-slate-500 text-sm">Execute um treinamento em <strong>Modelos ML</strong> para coletar dados.</p>
        </div>
      ) : (
        <div className="relative">
          {loadingCandles && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-slate-900/60 rounded-lg">
              <span className="text-slate-300 text-sm animate-pulse">Atualizando...</span>
            </div>
          )}
          <div className={`grid gap-3 ${hasAuxiliaryCharts ? 'lg:grid-cols-[minmax(0,0.58fr)_minmax(0,0.42fr)]' : ''}`}>
            <div className={`${hasAuxiliaryCharts ? 'order-1' : 'order-1'} min-w-0 rounded-lg border border-slate-700 bg-slate-900/40 p-2`}>
              <div className="mb-2 px-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-400">
                Principal
              </div>
              <div className="flex items-stretch gap-2">
                <div className="hidden w-36 shrink-0 overflow-hidden rounded-lg border border-slate-700 bg-slate-900/70 sm:block">
                  <div
                    style={{ height: mainChartHeight }}
                    className="flex flex-col items-start gap-1 px-2 pt-2"
                  >
                    <span className="rounded bg-slate-800 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-300">
                      Principal
                    </span>
                    <div className="flex flex-col gap-1 text-[10px] text-slate-400">
                      {mainPaneLegend?.lines.map((line) => (
                        <div key={`principal:${line.label}`} className="flex items-center gap-1">
                          <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ backgroundColor: line.color }} />
                          <span>{line.label}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>

                <div className="min-w-0 flex-1">
                  <CandlestickChart
                    key={`${market}:${symbol}:${timeframe}:main`}
                    candles={liveCandles}
                    trades={trades}
                    symbol={symbol}
                    timeframe={timeframe}
                    overlayLines={mainOverlayLines}
                    height={mainChartHeight}
                    onCrosshairTimeChange={setSyncedCrosshairTime}
                  />
                </div>
              </div>
            </div>

            {hasAuxiliaryCharts && (
              <div className="order-2 min-w-0 rounded-lg border border-slate-700 bg-slate-900/40 p-2">
                <div className="mb-2 px-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-400">
                  Secundários
                </div>
                <div className="flex items-stretch gap-2">
                  <div className="hidden w-36 shrink-0 overflow-hidden rounded-lg border border-slate-700 bg-slate-900/70 lg:block">
                    {auxiliaryPaneLegends.map((pane, idx) => (
                      <div
                        key={`${pane.paneIndex}:${pane.title}`}
                        style={{ height: secondaryPaneHeights[idx] ?? secondaryPaneHeight }}
                        className="flex flex-col items-start gap-1 border-b border-slate-800 px-2 pt-2 last:border-b-0"
                      >
                        <span className="rounded bg-slate-800 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-300">
                          {pane.title}
                        </span>
                        <div className="flex flex-col gap-1 text-[10px] text-slate-400">
                          {pane.lines.map((line) => (
                            <div key={`${pane.title}:${line.label}`} className="flex items-center gap-1">
                              <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ backgroundColor: line.color }} />
                              <span>{line.label}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>

                  <div className="min-w-0 flex-1">
                    <div className="overflow-hidden rounded-lg border border-slate-700 bg-slate-900/60">
                      {auxiliaryCharts.map((pane, idx) => (
                        <div key={pane.key} className="border-b border-slate-800 last:border-b-0">
                          <CandlestickChart
                            key={`${market}:${symbol}:${timeframe}:secondary:${pane.title}:${idx}`}
                            candles={liveCandles}
                            trades={[]}
                            symbol={symbol}
                            timeframe={timeframe}
                            overlayLines={pane.overlayLines}
                            height={secondaryPaneHeight}
                            showCandles={false}
                            showVolume={false}
                            syncCrosshairTime={syncedCrosshairTime}
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>

          {candlesError && (
            <div className="mt-2 rounded-md border border-amber-700/60 bg-amber-900/20 px-3 py-2 text-xs text-amber-200">
              Sem OHLCV para {symbol} em {timeframe}. Use 1h ou rode coleta/treino para esse timeframe.
            </div>
          )}
        </div>
      )}

      {/* Painel de preços em tempo real */}
      {prices.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-slate-300 mb-2">Preços em Tempo Real</h2>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-2">
            {prices.map((p) => {
              const isSelected = p.symbol === symbol
              const spread = ((p.ask - p.bid) / p.bid * 10000)
              return (
                <button
                  key={p.symbol}
                  onClick={() => {
                    setSymbol(p.symbol)
                    // Tenta detectar mercado pelo sufixo
                    if (p.symbol.endsWith('.pr') || p.symbol.endsWith('.lv')) {
                      const mkt = p.symbol.endsWith('.lv') ? 'CRIPTO' : 'FOREX'
                      setMarket(mkt)
                    }
                  }}
                  className={`rounded-lg border p-3 text-left transition-colors ${
                    isSelected
                      ? 'border-blue-500 bg-blue-900/20'
                      : 'border-slate-700 bg-slate-800/50 hover:border-slate-600'
                  }`}
                >
                  <div className="font-semibold text-white text-sm truncate">{p.symbol}</div>
                  <div className="font-mono text-emerald-400 text-base">{fmt(p.mid)}</div>
                  <div className="text-slate-500 text-xs mt-0.5">
                    Spread: {spread.toFixed(1)} pip
                  </div>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Trades recentes do símbolo */}
      {trades.length > 0 && (
        <div>
          <h2 className="text-sm font-semibold text-slate-300 mb-2">
            Trades — {symbol} <span className="text-slate-500">({trades.length})</span>
          </h2>
          <div className="overflow-x-auto rounded-lg border border-slate-700">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-slate-700 text-slate-400">
                  <th className="px-3 py-2 text-left">Lado</th>
                  <th className="px-3 py-2 text-right">Entrada</th>
                  <th className="px-3 py-2 text-right">Saída</th>
                  <th className="px-3 py-2 text-right">PnL</th>
                  <th className="px-3 py-2 text-left">Aberto em</th>
                  <th className="px-3 py-2 text-left">Motivo</th>
                </tr>
              </thead>
              <tbody>
                {trades.slice(0, 20).map((t) => {
                  const pnl = parseFloat(t.pnl_net ?? '0')
                  return (
                    <tr key={t.id} className="border-b border-slate-800 hover:bg-slate-800/40">
                      <td className="px-3 py-1.5">
                        <span className={`font-semibold ${t.side === 'BUY' ? 'text-emerald-400' : 'text-red-400'}`}>
                          {t.side}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono text-slate-300">
                        {parseFloat(t.entry_price).toFixed(5)}
                      </td>
                      <td className="px-3 py-1.5 text-right font-mono text-slate-300">
                        {t.exit_price ? parseFloat(t.exit_price).toFixed(5) : '—'}
                      </td>
                      <td className={`px-3 py-1.5 text-right font-mono font-semibold ${pctColor(pnl)}`}>
                        {t.pnl_net ? `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}` : '—'}
                      </td>
                      <td className="px-3 py-1.5 text-slate-400">
                        {new Date(t.open_at).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' })}
                      </td>
                      <td className="px-3 py-1.5 text-slate-500">{t.close_reason ?? '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
