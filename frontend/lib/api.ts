import { API_TOKEN, API_URL } from './constants'

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${API_TOKEN}`,
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`API ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

export interface HealthResponse {
  status: string
  version: string
  timestamp: string
  services: { db: string; redis: string }
}

export interface BotConfig {
  id: number
  market: string
  is_active: boolean
  mode: string
  exchange: string
  enabled_symbols: string[]
  risk_per_trade_pct: string
  max_open_trades: number
  max_drawdown_pct: string
  drawdown_alert_pct: string
  max_corr_threshold: string
  min_ml_confidence: string
  current_drawdown_1d: string
  current_drawdown_30d: string
  peak_capital: string | null
  drawdown_status: string
  paused_reason: string | null
  paused_at: string | null
  updated_at: string
}

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

export interface DrawdownInfo {
  market: string
  dd_1d: string
  dd_30d: string
  peak_capital: string
  current_equity: string
  status: string
}

export interface DailyMetrics {
  day_pnl: string
  trades_count: number
  win_rate: string
}

export interface EquityPoint {
  timestamp: string
  equity: string
  drawdown_pct: string
}

export interface TradeSummary {
  id: number
  market: string
  symbol: string
  side: string
  mode: string
  entry_price: string
  exit_price: string | null
  quantity: string
  pnl_gross: string | null
  pnl_net: string | null
  pnl_pct: string | null
  open_at: string
  close_at: string | null
  close_reason: string | null
  duration_sec: number | null
  ml_signal: string | null
  ml_confidence: string | null
}

export interface TradeHistoryResponse {
  trades: TradeSummary[]
  total: number
  page: number
  pages: number
}

export interface PerformanceMetrics {
  total_pnl: string
  trades_count: number
  win_rate: string
  profit_factor: string
  avg_pnl: string
  avg_duration_sec: number | null
}

export interface AuditLog {
  id: number
  market: string
  field: string
  old_value: string | null
  new_value: string | null
  changed_at: string
}

export interface BotConfigPatch {
  risk_per_trade_pct?: number
  max_open_trades?: number
  max_drawdown_pct?: number
  drawdown_alert_pct?: number
  max_corr_threshold?: number
}

export interface MLModel {
  id: number
  version: string
  symbol: string | null
  market: string
  status: 'active' | 'inactive' | 'retired'
  bt_profit_factor: number | null
  bt_win_rate: number | null
  bt_total_trades: number | null
  bt_sharpe_ratio: number | null
  bt_max_drawdown: number | null
  bt_total_return: number | null
  ml_accuracy: number | null
  train_samples: number
  is_approved: boolean
  trained_at: string
}

export interface TrainRequest {
  market?: string
  symbol?: string
  timeframe?: string
  profile?: 'didi' | 'robust'
}

export interface OptimizeTimeframesRequest {
  market?: string
  symbol?: string
  timeframes: string[]
  profile?: 'didi' | 'robust'
}

export interface TrainResponse {
  status: string
  message: string
  job_id: string
  profile?: 'didi' | 'robust'
}

export interface TrainSymbolStatus {
  market: string
  symbol: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'rejected'
  message: string
  started_at: string | null
  finished_at: string | null
}

export interface TrainJobStatus {
  job_id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  market: string | null
  symbol: string | null
  timeframe: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  total_symbols: number
  completed_symbols: number
  failed_symbols: number
  rejected_symbols: number
  approved_symbols: number
  progress_pct: number
  symbols: TrainSymbolStatus[]
}

export interface OHLCVCandle {
  time: number   // Unix timestamp (segundos)
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface PriceItem {
  symbol: string
  bid: number
  ask: number
  mid: number
  source?: 'live' | 'fallback'
}

export interface ImportSymbolsResponse {
  status: string
  market: string
  count: number
  symbols: string[]
}

export interface ImportSymbolsAllResponse {
  status: string
  imported: Record<string, { count: number; symbols: string[] }>
}

export interface ImportMarketWatchResponse extends ImportSymbolsAllResponse {
  subscribe_sent: boolean
  feed_symbols_count: number
  feed_symbols: string[]
  feed_symbols_omitted: number
}

export interface ImportAutogenResponse extends ImportSymbolsAllResponse {
  generated_file: string
  generated_symbols_count: number
  subscribe_sent: boolean
  feed_symbols_count: number
}

export interface EntryApproval {
  id: string
  status: 'pending' | 'approved' | 'rejected' | 'executing' | 'executed' | 'failed'
  market: string
  symbol: string
  side: 'buy' | 'sell'
  mode: 'paper' | 'live'
  timeframe: string
  entry_price: string
  stop_loss: string
  take_profit: string
  gain_safe?: string | null
  loss_safe?: string | null
  risk_per_unit?: string | null
  risk_reward?: string | null
  risk_amount?: string | null
  potential_gain?: string | null
  quantity: string
  suggested_quantity?: string | null
  criteria: Record<string, boolean>
  details: Record<string, string>
  analysis: string[]
  created_at: string
  expires_at: string
  updated_at?: string | null
  error?: string | null
}

export interface EntryApprovalChart {
  candles: OHLCVCandle[]
  overlays: Record<string, Array<{ time: number; value: number }>>
}

export interface EntryApprovalDecision {
  id: string
  status: string
}

export interface RemoveSymbolRequest {
  market: string
  symbol: string
  from_all_markets?: boolean
}

export interface RemoveSymbolResponse {
  status: string
  symbol: string
  requested_market: string
  from_all_markets: boolean
  removed_from: string[]
  skipped: string[]
}

export const api = {
  health: () => apiFetch<HealthResponse>('/health'),
  getConfig: (market: string) =>
    apiFetch<BotConfig>(`/api/v1/config/${market}`),
  patchConfig: (market: string, data: BotConfigPatch) =>
    apiFetch<BotConfig>(`/api/v1/config/${market}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  pauseBot: (market: string) =>
    apiFetch<{ status: string }>(`/api/v1/config/${market}/pause`, {
      method: 'POST',
    }),
  resumeBot: (market: string) =>
    apiFetch<{ status: string }>(`/api/v1/config/${market}/resume`, {
      method: 'POST',
    }),
  importSymbols: (market: string) =>
    apiFetch<ImportSymbolsResponse>(`/api/v1/config/${market}/import-symbols`, {
      method: 'POST',
    }),
  importSymbolsAll: () =>
    apiFetch<ImportSymbolsAllResponse>('/api/v1/config/import-symbols/all', {
      method: 'POST',
    }),
  importSymbolsFromMarketWatch: () =>
    apiFetch<ImportMarketWatchResponse>('/api/v1/config/import-symbols/market-watch', {
      method: 'POST',
    }),
  importSymbolsAutogen: () =>
    apiFetch<ImportAutogenResponse>('/api/v1/config/import-symbols/autogen-file-and-import', {
      method: 'POST',
    }),
  removeSymbolFromSystem: (body: RemoveSymbolRequest) =>
    apiFetch<RemoveSymbolResponse>('/api/v1/config/symbols/remove', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getAuditLog: (market: string, limit = 10) =>
    apiFetch<AuditLog[]>(`/api/v1/config/audit-log?market=${market}&limit=${limit}`),
  getPositions: () => apiFetch<Position[]>('/api/v1/positions'),
  getDrawdown: (market: string) => apiFetch<DrawdownInfo>(`/api/v1/risk/drawdown?market=${market}`),
  getDailyStats: (market?: string) =>
    apiFetch<DailyMetrics>(`/api/v1/trades/stats/daily${market ? `?market=${market}` : ''}`),
  getEquityHistory: (market: string, hours: number) =>
    apiFetch<EquityPoint[]>(`/api/v1/risk/equity-history?market=${market}&hours=${hours}`),
  getTradeHistory: (params: {
    page?: number
    per_page?: number
    market?: string
    result?: string
    start_date?: string
    end_date?: string
  }) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v != null && q.set(k, String(v)))
    return apiFetch<TradeHistoryResponse>(`/api/v1/trades/history?${q.toString()}`)
  },
  getPerformance: (params: { market?: string; start_date?: string; end_date?: string }) => {
    const q = new URLSearchParams()
    Object.entries(params).forEach(([k, v]) => v != null && q.set(k, String(v)))
    return apiFetch<PerformanceMetrics>(`/api/v1/trades/stats/performance?${q.toString()}`)
  },
  getModels: (market?: string) => {
    const q = market ? `?market=${market}` : ''
    return apiFetch<MLModel[]>(`/api/v1/ml/models${q}`)
  },
  triggerTrain: (body: TrainRequest) =>
    apiFetch<TrainResponse>('/api/v1/ml/train', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  triggerOptimizeTimeframes: (body: OptimizeTimeframesRequest) =>
    apiFetch<TrainResponse>('/api/v1/ml/train/optimize-timeframes', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getTrainStatus: (jobId: string) =>
    apiFetch<TrainJobStatus>(`/api/v1/ml/train/status/${encodeURIComponent(jobId)}`),
  downloadTrainReportCsv: async (jobId: string) => {
    const res = await fetch(`${API_URL}/api/v1/ml/train/report/${encodeURIComponent(jobId)}.csv`, {
      headers: {
        Authorization: `Bearer ${API_TOKEN}`,
      },
    })
    if (!res.ok) {
      const text = await res.text()
      throw new Error(`API ${res.status}: ${text}`)
    }
    return res.text()
  },
  getOHLCV: (symbol: string, timeframe: string, market: string, limit = 500) =>
    apiFetch<OHLCVCandle[]>(
      `/api/v1/market/ohlcv?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&market=${market}&limit=${limit}`
    ),
  getPrices: () => apiFetch<PriceItem[]>('/api/v1/market/prices'),
  getAvailableSymbols: () => apiFetch<Record<string, string[]>>('/api/v1/market/symbols'),
  getEntryApprovals: (status?: EntryApproval['status']) =>
    apiFetch<EntryApproval[]>(`/api/v1/approvals/entries${status ? `?status=${status}` : ''}`),
  getEntryApprovalChart: (approvalId: string) =>
    apiFetch<EntryApprovalChart>(`/api/v1/approvals/entries/${encodeURIComponent(approvalId)}/chart`),
  approveEntry: (approvalId: string, quantity?: string) =>
    apiFetch<EntryApprovalDecision>(`/api/v1/approvals/entries/${encodeURIComponent(approvalId)}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(quantity ? { quantity } : {}),
    }),
  rejectEntry: (approvalId: string) =>
    apiFetch<EntryApprovalDecision>(`/api/v1/approvals/entries/${encodeURIComponent(approvalId)}/reject`, {
      method: 'POST',
    }),
}
