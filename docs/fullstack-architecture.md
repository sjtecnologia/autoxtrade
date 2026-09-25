# autoxtrade — Full-Stack Architecture Document

**Versão:** 1.0  
**Data:** 28 de abril de 2026  
**Status:** Rascunho  
**Agente:** Architect (Winston / BMad)  
**Inputs:** docs/prd.md v1.0 + docs/front-end-spec.md v1.0

---

## Change Log

| Data | Versão | Descrição | Autor |
|---|---|---|---|
| 28/04/2026 | 1.0 | Versão inicial — arquitetura full-stack completa | Architect (Winston / BMad) |

---

## 1. Visão Geral da Arquitetura

### 1.1 Estilo Arquitetural

**Monolith Modular** — todos os componentes do backend rodam no mesmo processo Python principal, com separação lógica clara por módulo. Não são microsserviços. Justificativa:

- **Complexidade operacional** de microsserviços é desnecessária para uso pessoal com um desenvolvedor
- **Latência interna** zero entre trading engine e risk manager (chamadas de função, não HTTP)
- **Deploy simples** — um único `docker compose up` levanta tudo
- **Debug facilitado** — stack traces atravessam módulos sem barreiras de rede
- **Evolução segura** — monolith pode ser decomposto em serviços no futuro se houver escala

Componentes que rodam como processos separados (necessário por natureza técnica):
- **Celery Worker** — tarefas assíncronas (retreinamento ML, relatórios) em processo separado
- **PostgreSQL** — banco de dados relacional
- **Redis** — cache e broker de filas

### 1.2 Diagrama de Componentes

```
┌─────────────────────────────────────────────────────────────────────────┐
│  VPS (Windows Server 2022 ou Ubuntu 22.04 LTS com Wine para MT5)        │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  Docker Compose Network: autoxtrade_net                          │   │
│  │                                                                  │   │
│  │  ┌─────────────────┐    ┌─────────────────┐                     │   │
│  │  │   frontend      │    │    nginx         │◄── HTTPS :443       │   │
│  │  │  (Next.js)      │    │  (reverse proxy) │    (Let's Encrypt)  │   │
│  │  │   :3000         │◄───│                  │                     │   │
│  │  └─────────────────┘    └────────┬─────────┘                    │   │
│  │                                  │ /api/*  /ws                  │   │
│  │  ┌───────────────────────────────▼──────────────────────────┐   │   │
│  │  │  backend (FastAPI) :8000                                  │   │   │
│  │  │                                                           │   │   │
│  │  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │   │   │
│  │  │  │ Trading      │  │  ML Service  │  │  API Router   │  │   │   │
│  │  │  │ Engine       │  │  (inference) │  │  (REST + WS)  │  │   │   │
│  │  │  │              │  │              │  │               │  │   │   │
│  │  │  │ ┌──────────┐ │  │ ┌──────────┐│  │ ┌───────────┐ │  │   │   │
│  │  │  │ │Connector │ │  │ │  Model   ││  │ │ /positions│ │  │   │   │
│  │  │  │ │ Binance  │ │  │ │  Loader  ││  │ │ /history  │ │  │   │   │
│  │  │  │ │ (CCXT)   │ │  │ └──────────┘│  │ │ /metrics  │ │  │   │   │
│  │  │  │ └──────────┘ │  │ ┌──────────┐│  │ │ /bots     │ │  │   │   │
│  │  │  │ ┌──────────┐ │  │ │Predictor ││  │ │ /backtest │ │  │   │   │
│  │  │  │ │ Connector│ │  │ └──────────┘│  │ │ /config   │ │  │   │   │
│  │  │  │ │   MT5    │ │  └──────────────┘  │ │ WS /ws    │ │  │   │   │
│  │  │  │ │ (fase 1b)│ │  ┌──────────────┐  │ └───────────┘ │  │   │   │
│  │  │  │ └──────────┘ │  │ Risk Manager │  └───────────────┘  │   │   │
│  │  │  │ ┌──────────┐ │  │              │  ┌───────────────┐  │   │   │
│  │  │  │ │  Order   │ │  │ ┌──────────┐ │  │ Notifications │  │   │   │
│  │  │  │ │ Executor │ │  │ │ Position │ │  │ (Telegram)    │  │   │   │
│  │  │  │ └──────────┘ │  │ │  Sizer   │ │  └───────────────┘  │   │   │
│  │  │  └──────────────┘  │ ├──────────┤ │                      │   │   │
│  │  │                    │ │Drawdown  │ │                      │   │   │
│  │  │                    │ │ Monitor  │ │                      │   │   │
│  │  │                    │ ├──────────┤ │                      │   │   │
│  │  │                    │ │Correl.   │ │                      │   │   │
│  │  │                    │ │ Check    │ │                      │   │   │
│  │  │                    │ └──────────┘ │                      │   │   │
│  │  │                    └──────────────┘                      │   │   │
│  │  └───────────────────────────────────────────────────────────┘   │   │
│  │                                                                  │   │
│  │  ┌──────────────────────┐   ┌──────────────────────────────┐    │   │
│  │  │  celery_worker       │   │  celery_beat                 │    │   │
│  │  │  (async tasks)       │   │  (scheduler)                 │    │   │
│  │  │  - ML training       │   │  - weekly retrain            │    │   │
│  │  │  - backtesting       │   │  - daily report 23h          │    │   │
│  │  │  - daily reports     │   │  - data update 03h           │    │   │
│  │  └──────────────────────┘   └──────────────────────────────┘    │   │
│  │                                                                  │   │
│  │  ┌────────────────────┐   ┌────────────────────────────────┐    │   │
│  │  │  postgres :5432    │   │  redis :6379                   │    │   │
│  │  │  (persistent data) │   │  - WebSocket pub/sub           │    │   │
│  │  │  Volume: pg_data   │   │  - Celery broker/result        │    │   │
│  │  └────────────────────┘   │  - Market data cache (TTL 5s)  │    │   │
│  │                           └────────────────────────────────┘    │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  MetaTrader 5 Terminal (processo nativo Windows — fora do Docker)│   │
│  │  Conectado via MetaTrader5 Python lib (named pipe / COM)         │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘

Externo:
  Binance WebSocket API ──► Trading Engine (CCXT)
  Telegram Bot API      ◄── Notifications Module
  Exchange OHLCV API    ──► ML Data Pipeline
```

---

## 2. Modelo de Dados — PostgreSQL

### 2.1 Schema Completo

#### Tabela: `trades`

```sql
CREATE TABLE trades (
    id              BIGSERIAL PRIMARY KEY,
    market          VARCHAR(10)     NOT NULL,  -- 'CRIPTO', 'B3', 'FOREX'
    exchange        VARCHAR(30)     NOT NULL,  -- 'binance', 'mt5_xp', 'mt5_modal'
    symbol          VARCHAR(20)     NOT NULL,  -- 'BTC/USDT', 'WIN$N', 'EURUSD'
    side            VARCHAR(5)      NOT NULL,  -- 'LONG', 'SHORT'
    status          VARCHAR(10)     NOT NULL,  -- 'open', 'closed', 'cancelled'
    mode            VARCHAR(10)     NOT NULL DEFAULT 'live',  -- 'live', 'paper'

    -- Abertura
    entry_price     NUMERIC(20, 8)  NOT NULL,
    quantity        NUMERIC(20, 8)  NOT NULL,
    entry_value     NUMERIC(20, 2)  NOT NULL,   -- entry_price * quantity em R$
    stop_loss       NUMERIC(20, 8)  NOT NULL,
    take_profit     NUMERIC(20, 8),
    open_at         TIMESTAMPTZ     NOT NULL,
    open_order_id   VARCHAR(100),               -- ID da ordem na exchange

    -- Fechamento
    exit_price      NUMERIC(20, 8),
    exit_value      NUMERIC(20, 2),
    close_at        TIMESTAMPTZ,
    close_order_id  VARCHAR(100),
    close_reason    VARCHAR(20),  -- 'TP_HIT', 'SL_HIT', 'MANUAL', 'DD_LIMIT', 'EXPIRY'

    -- Resultado
    pnl_gross       NUMERIC(20, 2),  -- sem custos
    commission      NUMERIC(20, 2) DEFAULT 0,
    pnl_net         NUMERIC(20, 2),  -- pnl_gross - commission
    pnl_pct         NUMERIC(10, 4),  -- % em relação ao entry_value
    duration_sec    INTEGER,         -- duração em segundos

    -- ML Metadata
    model_version   VARCHAR(50),     -- 'rf_v20260428_143022'
    ml_signal       VARCHAR(10),     -- 'LONG', 'SHORT', 'NEUTRO'
    ml_confidence   NUMERIC(5, 4),   -- 0.0000 a 1.0000
    market_regime   VARCHAR(20),     -- 'UPTREND', 'DOWNTREND', 'SIDEWAYS'

    -- Risk metadata
    risk_amount     NUMERIC(20, 2),  -- capital em risco na operação
    risk_pct        NUMERIC(5, 2),   -- % do capital em risco

    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_trades_market     ON trades(market);
CREATE INDEX idx_trades_symbol     ON trades(symbol);
CREATE INDEX idx_trades_status     ON trades(status);
CREATE INDEX idx_trades_open_at    ON trades(open_at DESC);
CREATE INDEX idx_trades_mode       ON trades(mode);
CREATE INDEX idx_trades_model      ON trades(model_version);
```

#### Tabela: `ml_models`

```sql
CREATE TABLE ml_models (
    id              BIGSERIAL PRIMARY KEY,
    version         VARCHAR(50)     NOT NULL UNIQUE,  -- 'rf_v20260428_143022'
    market          VARCHAR(10)     NOT NULL,          -- 'CRIPTO', 'B3', 'FOREX'
    symbol          VARCHAR(20),                       -- NULL = modelo global
    model_type      VARCHAR(20)     NOT NULL,          -- 'random_forest', 'lstm', 'ensemble'
    status          VARCHAR(20)     NOT NULL DEFAULT 'inactive',
                                    -- 'training', 'validating', 'active', 'inactive', 'rejected'

    -- Arquivo do modelo
    file_path       VARCHAR(255)    NOT NULL,  -- caminho no filesystem do container
    scaler_path     VARCHAR(255),              -- caminho do StandardScaler salvo
    feature_list    JSONB           NOT NULL,  -- lista ordenada de features usadas

    -- Hiperparâmetros
    hyperparams     JSONB           NOT NULL,  -- ex: {"n_estimators": 200, "max_depth": 10}

    -- Dados de treino
    train_start     DATE            NOT NULL,
    train_end       DATE            NOT NULL,
    train_samples   INTEGER         NOT NULL,

    -- Métricas de backtesting (walk-forward médio)
    bt_profit_factor    NUMERIC(8, 4),
    bt_sharpe_ratio     NUMERIC(8, 4),
    bt_win_rate         NUMERIC(6, 4),
    bt_max_drawdown     NUMERIC(6, 4),
    bt_total_trades     INTEGER,
    bt_total_return     NUMERIC(10, 4),

    -- Métricas de classificação ML
    ml_accuracy         NUMERIC(6, 4),
    ml_precision_long   NUMERIC(6, 4),
    ml_recall_long      NUMERIC(6, 4),
    ml_precision_short  NUMERIC(6, 4),
    ml_recall_short     NUMERIC(6, 4),

    -- Aprovação/Rejeição
    is_approved         BOOLEAN         NOT NULL DEFAULT FALSE,
    rejection_reason    TEXT,
    approved_at         TIMESTAMPTZ,

    trained_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    activated_at    TIMESTAMPTZ,
    deactivated_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_models_market  ON ml_models(market);
CREATE INDEX idx_models_status  ON ml_models(status);
CREATE INDEX idx_models_version ON ml_models(version);
```

#### Tabela: `bot_config`

```sql
CREATE TABLE bot_config (
    id              BIGSERIAL PRIMARY KEY,
    market          VARCHAR(10)     NOT NULL UNIQUE,  -- 'CRIPTO', 'B3', 'FOREX'

    -- Estado
    is_active       BOOLEAN         NOT NULL DEFAULT FALSE,
    mode            VARCHAR(10)     NOT NULL DEFAULT 'paper',  -- 'live', 'paper'
    active_model_id BIGINT REFERENCES ml_models(id),

    -- Exchange
    exchange        VARCHAR(30)     NOT NULL,  -- 'binance', 'mt5_xp'
    enabled_symbols TEXT[]          NOT NULL DEFAULT '{}',  -- ['BTC/USDT', 'ETH/USDT']

    -- Parâmetros de risco
    risk_per_trade_pct  NUMERIC(5, 2)   NOT NULL DEFAULT 1.0,   -- 1%
    max_open_trades     SMALLINT        NOT NULL DEFAULT 3,
    max_drawdown_pct    NUMERIC(5, 2)   NOT NULL DEFAULT 15.0,
    drawdown_alert_pct  NUMERIC(5, 2)   NOT NULL DEFAULT 10.0,
    max_corr_threshold  NUMERIC(4, 2)   NOT NULL DEFAULT 0.70,
    min_ml_confidence   NUMERIC(4, 2)   NOT NULL DEFAULT 0.60,

    -- Estado de risco calculado (atualizado em runtime)
    current_drawdown_1d     NUMERIC(6, 2)   NOT NULL DEFAULT 0,
    current_drawdown_30d    NUMERIC(6, 2)   NOT NULL DEFAULT 0,
    peak_capital            NUMERIC(20, 2),
    drawdown_status         VARCHAR(10)     NOT NULL DEFAULT 'OK',
                            -- 'OK', 'WARNING', 'PAUSED'
    paused_reason           TEXT,
    paused_at               TIMESTAMPTZ,

    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);
```

#### Tabela: `equity_snapshots`

```sql
CREATE TABLE equity_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_at     TIMESTAMPTZ     NOT NULL,
    market          VARCHAR(10),           -- NULL = total consolidado
    equity          NUMERIC(20, 2)  NOT NULL,
    open_pnl        NUMERIC(20, 2)  NOT NULL DEFAULT 0,
    realized_pnl    NUMERIC(20, 2)  NOT NULL DEFAULT 0,
    drawdown_1d     NUMERIC(6, 2)   NOT NULL DEFAULT 0,
    drawdown_30d    NUMERIC(6, 2)   NOT NULL DEFAULT 0,
    open_trades     SMALLINT        NOT NULL DEFAULT 0
);

CREATE INDEX idx_equity_snapshot_at ON equity_snapshots(snapshot_at DESC);
CREATE INDEX idx_equity_market      ON equity_snapshots(market, snapshot_at DESC);
```

#### Tabela: `trade_events`

```sql
CREATE TABLE trade_events (
    id          BIGSERIAL PRIMARY KEY,
    trade_id    BIGINT      NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    event_type  VARCHAR(30) NOT NULL,
                -- 'SIGNAL_GENERATED', 'ORDER_SENT', 'ORDER_EXECUTED',
                -- 'OCO_SET', 'TP_HIT', 'SL_HIT', 'MANUAL_CLOSE',
                -- 'TELEGRAM_SENT', 'ERROR'
    payload     JSONB,          -- dados adicionais do evento
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_events_trade_id   ON trade_events(trade_id);
CREATE INDEX idx_events_type       ON trade_events(event_type);
CREATE INDEX idx_events_occurred   ON trade_events(occurred_at DESC);
```

#### Tabela: `config_audit_log`

```sql
CREATE TABLE config_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    market      VARCHAR(10),
    field_name  VARCHAR(50)     NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    changed_by  VARCHAR(50)     NOT NULL DEFAULT 'owner'  -- futuro: user_id
);
```

### 2.2 Diagrama Entidade-Relacionamento

```
bot_config ──(active_model_id)──► ml_models
                                      ▲
                                      │ model_version
                                   trades
                                      │
                                      ├──► trade_events
                                      │
                                   equity_snapshots (calculado)
                                   config_audit_log (auditoria)
```

---

## 3. Contrato de API

### 3.1 Autenticação

Bearer token estático configurado em variável de ambiente `API_SECRET_TOKEN`. Todas as requisições ao backend exigem:

```
Authorization: Bearer <token>
```

O token é gerado na primeira execução (`secrets.token_hex(32)`) e armazenado em `.env`. O frontend lê o token de variável de ambiente `NEXT_PUBLIC_API_TOKEN` — definida em build time no Docker.

> **Nota de segurança:** Token não expira automaticamente no MVP. Rotação manual via `.env` + restart do container. Nunca versionar o `.env`.

### 3.2 Endpoints REST

**Base URL:** `https://[domínio]/api/v1`

#### Health & Status

```
GET  /health
→ 200 { "status": "ok", "version": "0.1.0", "timestamp": "ISO8601",
         "services": { "db": "ok", "redis": "ok", "binance": "ok" } }
```

#### Posições

```
GET  /positions
→ 200 { "positions": [ Position[] ], "total_open_pnl": float }

GET  /positions/{id}
→ 200 Position

DELETE /positions/{id}          # fecha manualmente
→ 200 { "status": "closing", "estimated_pnl": float }
```

**Position object:**
```json
{
  "id": 1042,
  "market": "CRIPTO",
  "symbol": "BTC/USDT",
  "side": "LONG",
  "entry_price": 287450.00,
  "current_price": 290120.00,
  "quantity": 0.00234,
  "entry_value": 672.63,
  "stop_loss": 282900.00,
  "take_profit": 294000.00,
  "open_pnl": 6.25,
  "open_pnl_pct": 0.0093,
  "open_at": "2026-04-28T14:32:18Z",
  "duration_sec": 8027,
  "model_version": "rf_v20260428_143022",
  "ml_confidence": 0.78
}
```

#### Histórico de Trades

```
GET  /trades?market=CRIPTO&from=2026-04-01&to=2026-04-28&result=profit&page=1&per_page=50
→ 200 {
    "trades": [ Trade[] ],
    "total": 142,
    "page": 1,
    "per_page": 50,
    "summary": {
      "total_pnl": 2841.22,
      "win_rate": 0.634,
      "profit_factor": 1.87,
      "sharpe_ratio": 1.64,
      "max_drawdown": 0.087,
      "total_trades": 142
    }
  }

GET  /trades/{id}
→ 200 TradeDetail (inclui trade_events[])

GET  /trades/export?...  # mesmos filtros
→ 200 text/csv
```

#### Métricas & Equity

```
GET  /metrics?period=30d&market=CRIPTO
→ 200 {
    "win_rate": 0.634,
    "profit_factor": 1.87,
    "sharpe_ratio": 1.64,
    "max_drawdown": 0.087,
    "avg_rr": 1.72,
    "total_trades": 142,
    "total_pnl": 2841.22,
    "by_market": [ { "market": "CRIPTO", ... } ]
  }

GET  /equity?period=7d&market=CRIPTO
→ 200 {
    "equity_curve": [
      { "ts": "ISO8601", "equity": 12847.32, "drawdown": 0.034 }
    ],
    "initial_capital": 10000.00,
    "current_equity": 12847.32,
    "drawdown_1d": 0.012,
    "drawdown_30d": 0.034
  }
```

#### Configuração & Bots

```
GET  /config
→ 200 { "configs": [ BotConfig[] ] }

GET  /config/{market}
→ 200 BotConfig

PATCH /config/{market}
Body: { "is_active": bool, "mode": "live"|"paper", "risk_per_trade_pct": float, ... }
→ 200 BotConfig updated

POST /config/{market}/pause
→ 200 { "status": "paused", "market": "CRIPTO" }

POST /config/{market}/resume
→ 200 { "status": "active", "market": "CRIPTO" }
```

#### Modelos ML

```
GET  /ml/models?market=CRIPTO
→ 200 { "models": [ MLModel[] ] }

GET  /ml/models/active?market=CRIPTO
→ 200 MLModel

POST /ml/models/{version}/activate
→ 200 { "activated": "rf_v20260505", "deactivated": "rf_v20260428" }

POST /ml/predict
Body: { "market": "CRIPTO", "symbol": "BTC/USDT", "candles": OHLCV[] }
→ 200 { "signal": "LONG"|"SHORT"|"NEUTRO", "confidence": 0.78, "model_version": "..." }

POST /ml/retrain           # dispara retreinamento via Celery
→ 202 { "task_id": "uuid", "status": "queued" }

GET  /ml/tasks/{task_id}   # status da tarefa assíncrona
→ 200 { "task_id": "uuid", "status": "pending"|"running"|"completed"|"failed",
         "progress": 0.65, "result": MLModel | null, "error": null }
```

#### Backtesting

```
POST /backtest/run
Body: {
  "market": "CRIPTO",
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "from_date": "2024-01-01",
  "to_date": "2026-04-28",
  "strategy": "walk_forward",
  "train_months": 12,
  "test_months": 3,
  "initial_capital": 10000,
  "risk_per_trade_pct": 1.0,
  "max_open_trades": 3
}
→ 202 { "task_id": "uuid" }

GET  /backtest/results/{task_id}
→ 200 {
    "status": "completed",
    "summary": { ... métricas ... },
    "equity_curve": [ { "ts": ..., "equity": ... } ],
    "trades": [ Trade[] ],
    "is_approved": true,
    "rejection_reason": null
  }
```

#### Risk

```
GET  /risk/drawdown
→ 200 {
    "drawdown_1d": 0.012,
    "drawdown_30d": 0.034,
    "status": "OK"|"WARNING"|"PAUSED",
    "peak_capital": 13200.00,
    "current_capital": 12847.32
  }

GET  /risk/sizing?market=CRIPTO&symbol=BTC/USDT&stop_pct=1.5
→ 200 {
    "suggested_quantity": 0.00234,
    "position_value": 672.63,
    "risk_amount": 6.73,
    "risk_pct": 1.0
  }
```

### 3.3 Eventos WebSocket

**Endpoint:** `wss://[domínio]/ws`  
**Autenticação:** Query param `?token=<API_SECRET_TOKEN>`  
**Formato:** JSON com campo `type` discriminando o evento

#### Eventos Emitidos pelo Servidor → Frontend

```jsonc
// Atualização de preço em tempo real (a cada candle fechado ou tick)
{ "type": "PRICE_UPDATE",
  "symbol": "BTC/USDT",
  "price": 290120.00,
  "ts": "ISO8601" }

// Atualização de P&L de posição aberta
{ "type": "POSITION_UPDATE",
  "trade_id": 1042,
  "current_price": 290120.00,
  "open_pnl": 6.25,
  "open_pnl_pct": 0.0093 }

// Nova posição aberta
{ "type": "POSITION_OPENED",
  "trade": { ...Position object... } }

// Posição fechada
{ "type": "POSITION_CLOSED",
  "trade_id": 1042,
  "close_reason": "TP_HIT",
  "pnl_net": 265.66,
  "pnl_pct": 0.0093 }

// Alerta de drawdown
{ "type": "DRAWDOWN_ALERT",
  "level": "WARNING"|"PAUSED",
  "drawdown_30d": 0.104,
  "market": "CRIPTO"|"ALL" }

// Status do bot alterado
{ "type": "BOT_STATUS_CHANGED",
  "market": "CRIPTO",
  "status": "active"|"paused"|"paper" }

// Novo modelo ML disponível
{ "type": "MODEL_READY",
  "version": "rf_v20260505_020000",
  "profit_factor": 1.91,
  "market": "CRIPTO",
  "is_approved": true }

// Heartbeat (a cada 30s)
{ "type": "PING", "ts": "ISO8601" }
```

#### Comandos Enviados pelo Frontend → Servidor (via WS)

```jsonc
// Subscrever a símbolos específicos
{ "type": "SUBSCRIBE", "symbols": ["BTC/USDT", "ETH/USDT"] }

// Pong em resposta ao ping
{ "type": "PONG" }
```

---

## 4. Fluxo de Dados — Pipeline Completo

### 4.1 Fluxo de Trading (tempo real)

```
Binance WebSocket
      │ OHLCV candle fechado
      ▼
Trading Engine (loop principal)
      │
      ├─1─► Risk Manager: drawdown OK? posições < max? correlação < threshold?
      │         └─ NÃO → abortar, logar razão
      │         └─ SIM → continuar
      │
      ├─2─► ML Service: predict(candles[-50:], features)
      │         └─ confidence < 0.6 → NEUTRO → abortar
      │         └─ LONG ou SHORT → continuar
      │
      ├─3─► Risk Manager: calcular position size
      │         quantity = (capital × risk_pct) / distância_stop
      │
      ├─4─► Order Executor: enviar ordem OCO à Binance
      │         POST /api/v3/order/oco {symbol, side, quantity, price, stopPrice, stopLimitPrice}
      │
      ├─5─► Database: INSERT trades (status='open')
      │
      ├─6─► Redis PUBLISH: evento POSITION_OPENED
      │         └─► WebSocket Handler → broadcast para frontend
      │
      └─7─► Telegram: enviar notificação de abertura
```

### 4.2 Fluxo de Fechamento de Posição

```
Binance WebSocket (order update)
      │ OCO executado (TP ou SL atingido)
      ▼
Order Monitor
      │
      ├─1─► Database: UPDATE trades (status='closed', exit_price, pnl_net, close_reason)
      ├─2─► Database: INSERT trade_events (tipo: TP_HIT ou SL_HIT)
      ├─3─► Risk Manager: recalcular drawdown atual
      ├─4─► Database: INSERT equity_snapshots
      ├─5─► Redis PUBLISH: evento POSITION_CLOSED
      │         └─► WebSocket Handler → broadcast frontend
      └─6─► Telegram: enviar notificação de fechamento com resultado
```

### 4.3 Fluxo do Pipeline ML (semanal, assíncrono)

```
Celery Beat (toda segunda-feira às 02h BRT)
      │ task: retrain_model.apply_async(market='CRIPTO')
      ▼
Celery Worker
      │
      ├─1─► Data Collector: baixar OHLCV incremental via CCXT
      │         freqtrade download-data --pairs BTC/USDT --timeframes 1h
      │
      ├─2─► Feature Engineer: calcular indicadores técnicos
      │         RSI, MACD, BB, EMA(9,21,50,200), ATR, OBV, Stoch
      │         + regime features (ADX, volume ratio)
      │         + target: retorno futuro binarizado (LONG/SHORT/NEUTRO)
      │
      ├─3─► Model Trainer: walk-forward cross-validation (4 folds)
      │         Fold: treino 12m → teste 3m
      │         GridSearchCV para hiperparâmetros
      │
      ├─4─► Validator: calcular métricas em dados out-of-sample
      │         Profit Factor médio nos folds de teste
      │         └─ PF < 1.3 → status='rejected', logar, manter modelo anterior
      │         └─ PF ≥ 1.3 → aprovar, continuar
      │
      ├─5─► Model Saver: persistir modelo em disco
      │         pickle.dump(model) → /app/ml/models/rf_v{timestamp}.pkl
      │         pickle.dump(scaler) → /app/ml/models/scaler_v{timestamp}.pkl
      │
      ├─6─► Database: INSERT ml_models (status='inactive', métricas)
      │
      ├─7─► Redis PUBLISH: evento MODEL_READY
      │         └─► WebSocket Handler → notifica frontend
      │
      └─8─► Telegram: "✅ Novo modelo rf_v... disponível (PF: 1.91)"
            [Owner decide quando ativar via dashboard]
```

### 4.4 Fluxo de Drawdown Automático

```
Risk Monitor (loop a cada 60 segundos)
      │
      ├─1─► Calcular equity atual: capital_em_conta + open_pnl_não_realizado
      ├─2─► Calcular drawdown_30d: (peak_capital - equity_atual) / peak_capital
      │
      ├─3─► drawdown_30d ≥ 10%?
      │         └─ SIM: UPDATE bot_config (drawdown_status='WARNING')
      │                Redis PUBLISH: DRAWDOWN_ALERT {level: 'WARNING'}
      │                Telegram: ⚠️ alerta
      │
      └─4─► drawdown_30d ≥ 15%?
                └─ SIM: UPDATE bot_config (is_active=FALSE, drawdown_status='PAUSED')
                         Redis PUBLISH: DRAWDOWN_ALERT {level: 'PAUSED'}
                         Telegram: 🚨 trading pausado
                         Trading Engine: para de gerar novos sinais
```

---

## 5. Estrutura de Diretórios — Repositório Completo

```
autoxtrade/
├── .env.example                    # template de variáveis de ambiente (versionado)
├── .env                            # variáveis reais (NÃO versionado — no .gitignore)
├── .gitignore
├── docker-compose.yml
├── docker-compose.override.yml     # overrides para desenvolvimento local
├── README.md
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   │       └── 0001_initial_schema.py
│   │
│   ├── main.py                     # entrypoint FastAPI
│   ├── config.py                   # Settings (pydantic-settings, lê .env)
│   ├── database.py                 # engine SQLAlchemy, session factory
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── router.py               # inclui todos os routers
│   │   ├── auth.py                 # bearer token middleware
│   │   ├── websocket.py            # WS endpoint + broadcast
│   │   ├── routes/
│   │   │   ├── health.py
│   │   │   ├── positions.py
│   │   │   ├── trades.py
│   │   │   ├── metrics.py
│   │   │   ├── config.py
│   │   │   ├── ml.py
│   │   │   ├── backtest.py
│   │   │   └── risk.py
│   │   └── schemas/                # Pydantic models (request/response)
│   │       ├── trade.py
│   │       ├── config.py
│   │       ├── ml_model.py
│   │       └── metrics.py
│   │
│   ├── trading/
│   │   ├── __init__.py
│   │   ├── engine.py               # loop principal de trading
│   │   ├── connectors/
│   │   │   ├── base.py             # interface abstrata do connector
│   │   │   ├── binance.py          # CCXT Binance + WebSocket
│   │   │   └── mt5.py              # MetaTrader5 Python lib (fase 1b)
│   │   ├── order_executor.py       # constrói e envia ordens OCO
│   │   ├── order_monitor.py        # monitora updates de ordens via WS
│   │   └── paper_trader.py         # simulação de ordens em paper mode
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── manager.py              # orquestra todos os checks de risco
│   │   ├── position_sizer.py       # cálculo de tamanho de posição
│   │   ├── drawdown_monitor.py     # cálculo e enforcement de drawdown
│   │   └── correlation_checker.py  # correlação entre posições abertas
│   │
│   ├── ml/
│   │   ├── __init__.py
│   │   ├── pipeline.py             # orquestra treino completo
│   │   ├── data_collector.py       # download OHLCV via CCXT/MT5
│   │   ├── feature_engineer.py     # cálculo de indicadores + target
│   │   ├── trainer.py              # treino Random Forest + GridSearch
│   │   ├── validator.py            # walk-forward, métricas, aprovação
│   │   ├── predictor.py            # inferência em produção (carrega modelo)
│   │   ├── backtester.py           # simulação histórica completa
│   │   └── models/                 # modelos treinados (não versionados no git)
│   │       └── .gitkeep
│   │
│   ├── notifications/
│   │   ├── __init__.py
│   │   ├── telegram.py             # Telegram Bot API wrapper
│   │   └── templates.py            # formatação de mensagens
│   │
│   ├── tasks/
│   │   ├── __init__.py
│   │   ├── celery_app.py           # configuração Celery + Redis broker
│   │   ├── ml_tasks.py             # retrain_model, validate_model
│   │   ├── report_tasks.py         # daily_report
│   │   └── data_tasks.py           # update_market_data
│   │
│   └── tests/
│       ├── conftest.py
│       ├── unit/
│       │   ├── test_position_sizer.py
│       │   ├── test_drawdown_monitor.py
│       │   ├── test_correlation_checker.py
│       │   ├── test_order_executor.py
│       │   ├── test_feature_engineer.py
│       │   └── test_telegram.py
│       ├── integration/
│       │   ├── test_binance_connector.py  # com mocks
│       │   ├── test_api_endpoints.py
│       │   └── test_ml_pipeline.py
│       └── fixtures/
│           └── sample_ohlcv.parquet
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── next.config.ts
│   │
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx                # dashboard /
│   │   ├── positions/page.tsx
│   │   ├── history/page.tsx
│   │   ├── performance/page.tsx
│   │   ├── bots/page.tsx
│   │   ├── backtest/page.tsx
│   │   └── settings/page.tsx
│   │
│   ├── components/
│   │   ├── layout/
│   │   │   ├── Header.tsx
│   │   │   └── Sidebar.tsx
│   │   ├── ui/
│   │   │   ├── MetricCard.tsx
│   │   │   ├── StatusBadge.tsx
│   │   │   ├── PnlValue.tsx
│   │   │   ├── DrawdownBar.tsx
│   │   │   ├── DataTable.tsx
│   │   │   ├── ConfirmModal.tsx
│   │   │   └── ToastAlert.tsx
│   │   ├── charts/
│   │   │   ├── EquityCurve.tsx
│   │   │   ├── PnlHistogram.tsx
│   │   │   └── PerformanceHeatmap.tsx
│   │   └── features/               # componentes por feature (dashboard/, positions/, etc.)
│   │
│   ├── hooks/
│   │   ├── useWebSocket.ts
│   │   ├── usePositions.ts
│   │   └── useDrawdown.ts
│   │
│   ├── store/
│   │   └── botStore.ts             # Zustand global state
│   │
│   └── lib/
│       ├── api.ts
│       ├── formatters.ts
│       └── constants.ts
│
├── scripts/
│   ├── setup_vps.sh                # setup inicial do VPS (nginx, certbot, firewall)
│   ├── deploy.sh                   # pull + restart containers
│   ├── backup_db.sh                # backup manual do PostgreSQL
│   └── download_data.sh            # download inicial de dados históricos
│
└── docs/
    ├── brief.md
    ├── prd.md
    ├── front-end-spec.md
    └── fullstack-architecture.md   # este documento
```

---

## 6. Infraestrutura & Deploy

### 6.1 Especificação do VPS

| Requisito | Especificação Mínima | Recomendado |
|---|---|---|
| CPU | 2 vCPUs | 4 vCPUs |
| RAM | 4 GB | 8 GB |
| Disco | 40 GB SSD | 80 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS (fase 1 cripto) |
| OS fase 1b | Windows Server 2022 | Windows Server 2022 (para MT5) |
| Localização | Brasil (São Paulo) | Brasil (menor latência para B3) |
| Custo estimado | $15–25/mês | $30–50/mês |

Provedores recomendados: **Hetzner** (melhor custo-benefício Europa), **Vultr São Paulo**, **DigitalOcean São Paulo**, **Contabo** (custo mínimo).

### 6.2 Docker Compose — Produção

```yaml
# docker-compose.yml
version: "3.9"

services:
  nginx:
    image: nginx:1.25-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - certbot_certs:/etc/letsencrypt:ro
      - certbot_www:/var/www/certbot:ro
    depends_on:
      - frontend
      - backend
    restart: unless-stopped

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    environment:
      - NEXT_PUBLIC_API_URL=https://${DOMAIN}
      - NEXT_PUBLIC_WS_URL=wss://${DOMAIN}/ws
      - NEXT_PUBLIC_API_TOKEN=${API_SECRET_TOKEN}
    restart: unless-stopped

  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    environment:
      - DATABASE_URL=postgresql+asyncpg://autoxtrade:${DB_PASSWORD}@postgres:5432/autoxtrade
      - REDIS_URL=redis://redis:6379/0
      - API_SECRET_TOKEN=${API_SECRET_TOKEN}
      - BINANCE_API_KEY=${BINANCE_API_KEY}
      - BINANCE_API_SECRET=${BINANCE_API_SECRET}
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
      - ENVIRONMENT=production
    volumes:
      - ml_models:/app/backend/ml/models
      - trade_logs:/app/logs
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped

  celery_worker:
    build:
      context: ./backend
      dockerfile: Dockerfile
    command: celery -A tasks.celery_app worker --loglevel=info --concurrency=2
    environment:
      - DATABASE_URL=postgresql+asyncpg://autoxtrade:${DB_PASSWORD}@postgres:5432/autoxtrade
      - REDIS_URL=redis://redis:6379/0
      - BINANCE_API_KEY=${BINANCE_API_KEY}
      - BINANCE_API_SECRET=${BINANCE_API_SECRET}
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
    volumes:
      - ml_models:/app/backend/ml/models
    depends_on:
      - backend
      - redis
    restart: unless-stopped

  celery_beat:
    build:
      context: ./backend
      dockerfile: Dockerfile
    command: celery -A tasks.celery_app beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
    environment:
      - DATABASE_URL=postgresql+asyncpg://autoxtrade:${DB_PASSWORD}@postgres:5432/autoxtrade
      - REDIS_URL=redis://redis:6379/0
    depends_on:
      - backend
      - redis
    restart: unless-stopped

  postgres:
    image: postgres:16-alpine
    environment:
      - POSTGRES_USER=autoxtrade
      - POSTGRES_PASSWORD=${DB_PASSWORD}
      - POSTGRES_DB=autoxtrade
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U autoxtrade"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    command: redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 3
    restart: unless-stopped

volumes:
  pg_data:
  redis_data:
  ml_models:
  trade_logs:
  certbot_certs:
  certbot_www:

networks:
  default:
    name: autoxtrade_net
```

### 6.3 Configuração Nginx

```nginx
# nginx/nginx.conf
events { worker_connections 1024; }

http {
    # Rate limiting — protege contra brute force no token
    limit_req_zone $binary_remote_addr zone=api:10m rate=30r/m;

    upstream backend {
        server backend:8000;
    }

    upstream frontend {
        server frontend:3000;
    }

    # HTTP → redireciona para HTTPS
    server {
        listen 80;
        server_name _;

        location /.well-known/acme-challenge/ {
            root /var/www/certbot;
        }

        location / {
            return 301 https://$host$request_uri;
        }
    }

    # HTTPS
    server {
        listen 443 ssl http2;
        server_name ${DOMAIN};

        ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
        ssl_protocols       TLSv1.2 TLSv1.3;
        ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;

        # Security headers
        add_header Strict-Transport-Security "max-age=31536000" always;
        add_header X-Content-Type-Options nosniff;
        add_header X-Frame-Options DENY;
        add_header Content-Security-Policy "default-src 'self'; connect-src 'self' wss:;" always;

        # API REST
        location /api/ {
            limit_req zone=api burst=10 nodelay;
            proxy_pass http://backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }

        # WebSocket
        location /ws {
            proxy_pass http://backend;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 3600s;
        }

        # Frontend
        location / {
            proxy_pass http://frontend;
            proxy_set_header Host $host;
        }
    }
}
```

### 6.4 Variáveis de Ambiente (.env.example)

```bash
# .env.example — copiar para .env e preencher

# Sistema
DOMAIN=autoxtrade.seudominio.com
ENVIRONMENT=production
API_SECRET_TOKEN=          # gerar com: python -c "import secrets; print(secrets.token_hex(32))"

# Banco de dados
DB_PASSWORD=               # senha forte para PostgreSQL

# Binance (permissão: TRADE + READ_INFO, SEM withdrawal)
BINANCE_API_KEY=
BINANCE_API_SECRET=

# Telegram
TELEGRAM_BOT_TOKEN=        # obter com @BotFather
TELEGRAM_CHAT_ID=          # obter com @userinfobot

# MT5 (fase 1b)
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=                # ex: XPInvestimentos-Demo
```

### 6.5 Script de Setup Inicial do VPS

```bash
#!/bin/bash
# scripts/setup_vps.sh — executar como root no VPS

# 1. Atualizar sistema
apt-get update && apt-get upgrade -y

# 2. Instalar Docker
curl -fsSL https://get.docker.com | sh
systemctl enable docker

# 3. Instalar Docker Compose plugin
apt-get install -y docker-compose-plugin

# 4. Firewall (UFW)
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh                    # porta 22
ufw allow 80/tcp                 # HTTP (certbot)
ufw allow 443/tcp                # HTTPS
ufw --force enable

# 5. Desabilitar login root via senha (usar chave SSH)
sed -i 's/PermitRootLogin yes/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl reload sshd

# 6. Certbot para SSL
apt-get install -y certbot
certbot certonly --standalone -d ${DOMAIN} --email ${EMAIL} --agree-tos --non-interactive

# 7. Renovação automática do certificado
echo "0 3 * * * root certbot renew --quiet && docker compose -f /opt/autoxtrade/docker-compose.yml restart nginx" >> /etc/crontab

# 8. Criar diretório do projeto
mkdir -p /opt/autoxtrade
```

### 6.6 Script de Deploy

```bash
#!/bin/bash
# scripts/deploy.sh

set -e

cd /opt/autoxtrade

# Pull código mais recente
git pull origin main

# Build e restart dos containers
docker compose pull
docker compose build --no-cache
docker compose up -d --remove-orphans

# Executar migrações
docker compose exec backend alembic upgrade head

# Verificar saúde
sleep 10
docker compose ps
curl -f https://${DOMAIN}/health || echo "AVISO: health check falhou"

echo "Deploy concluído: $(date)"
```

### 6.7 Backup do Banco de Dados

```bash
#!/bin/bash
# scripts/backup_db.sh — executar via cron diariamente às 04h

BACKUP_DIR="/opt/autoxtrade/backups"
RETENTION_DAYS=30
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "${BACKUP_DIR}"

# Dump do PostgreSQL
docker compose exec -T postgres pg_dump -U autoxtrade autoxtrade | \
  gzip > "${BACKUP_DIR}/autoxtrade_${DATE}.sql.gz"

# Remover backups antigos
find "${BACKUP_DIR}" -name "*.sql.gz" -mtime +${RETENTION_DAYS} -delete

echo "Backup concluído: autoxtrade_${DATE}.sql.gz"
```

---

## 7. Segurança

### 7.1 Modelo de Ameaças (OWASP Top 10 aplicado)

| Ameaça | Controle implementado |
|---|---|
| **A01 Broken Access Control** | Bearer token em todas as rotas; Nginx rate limiting; sem rotas públicas (exceto `/health`) |
| **A02 Cryptographic Failures** | HTTPS obrigatório (TLS 1.2+); API keys nunca em texto plano; `.env` fora do git |
| **A03 Injection** | SQLAlchemy ORM com queries parametrizadas; Pydantic valida todos os inputs |
| **A05 Security Misconfiguration** | Firewall UFW; security headers no Nginx; PostgreSQL acessível apenas internamente na rede Docker |
| **A06 Vulnerable Components** | `pip-audit` + `npm audit` no CI; atualização regular de base images Docker |
| **A07 Auth Failures** | Rate limiting 30 req/min por IP no Nginx; sem endpoint de login (token estático) |
| **A09 Security Logging** | Todos os acessos à API logados; eventos críticos logados com timestamp |

### 7.2 Gestão de Credenciais

```
API Keys Binance
├─ Permissões: TRADE + READ_INFO apenas (sem withdrawal, sem transfer)
├─ IP Whitelist: configurar IP do VPS na Binance
└─ Armazenamento: variável de ambiente no .env (não versionado)

API Secret Token (dashboard)
├─ Gerado com secrets.token_hex(32) — 256 bits de entropia
├─ Não tem data de expiração no MVP
└─ Rotação: editar .env + docker compose restart

MT5 Credentials (fase 1b)
├─ Conta sem saldo real no início (conta demo)
└─ Armazenamento: variável de ambiente
```

### 7.3 Práticas de Segurança no Código

```python
# ❌ NUNCA fazer
api_key = "abc123"                              # hardcoded
query = f"SELECT * FROM trades WHERE id={id}"  # SQL injection

# ✅ SEMPRE fazer
from config import settings
api_key = settings.BINANCE_API_KEY              # via pydantic-settings

# SQLAlchemy ORM — parametrizado automaticamente
trade = await db.get(Trade, trade_id)
```

---

## 8. Monitoramento e Observabilidade

### 8.1 Logging

**Estrutura dos logs:**
```python
# Formato: JSON structured logging
{"ts": "2026-04-28T14:32:18Z", "level": "INFO", "module": "trading.engine",
 "event": "signal_generated", "symbol": "BTC/USDT", "signal": "LONG", "confidence": 0.78}
```

**Níveis de log:**
- `DEBUG`: dados de mercado raw (apenas em desenvolvimento)
- `INFO`: operações abertas/fechadas, modelos ativados, retreinamentos
- `WARNING`: correlação bloqueada, confidence baixa, drawdown em alerta
- `ERROR`: falhas de API, ordens rejeitadas, erros de conexão

**Retenção:** Arquivo de log rotacionado diariamente, retido por 90 dias.

### 8.2 Métricas de Saúde do Sistema

O endpoint `GET /health` retorna status de todos os componentes — monitorado via `docker compose healthcheck`. Em caso de falha, container reinicia automaticamente (`restart: unless-stopped`).

**Alertas via Telegram para eventos críticos:**
- Backend offline > 5 minutos
- Conexão com Binance perdida > 2 minutos
- Celery worker offline (retreinamento não executado)
- Erro crítico não tratado (exceção não capturada)

### 8.3 MVP — Sem Prometheus/Grafana

Para MVP pessoal, o overhead de Prometheus + Grafana não se justifica. O dashboard do autoxtrade serve como painel de monitoramento primário. Adicionar Prometheus em versão futura se houver necessidade.

---

## 9. Decisões de Arquitetura (ADRs)

### ADR-001: Monolith Modular vs Microsserviços

**Decisão:** Monolith modular  
**Justificativa:** Único desenvolvedor, uso pessoal, sem escala horizontal. Microsserviços introduzem complexidade operacional desnecessária (service discovery, comunicação inter-serviço, deploy coordenado). Módulos podem ser extraídos no futuro se necessário.

### ADR-002: Freqtrade como Framework Base vs Implementação do Zero

**Decisão:** Freqtrade (customizado)  
**Justificativa:** Freqtrade oferece backtesting nativo, download de dados, paper trading e integração CCXT out-of-the-box. Implementar do zero levaria meses a mais sem valor adicional. O ML pipeline é implementado como plugin personalizado do Freqtrade.

### ADR-003: Random Forest como Modelo Inicial vs LSTM

**Decisão:** Random Forest primeiro, LSTM em iteração futura  
**Justificativa:** Random Forest: treinamento rápido (minutos), interpretável (feature importance), robusto com poucos dados, sem necessidade de GPU. LSTM: melhor para sequências longas mas requer mais dados e tempo de treino. RF valida o pipeline completo antes de adicionar complexidade.

### ADR-004: PostgreSQL vs TimescaleDB para dados de série temporal

**Decisão:** PostgreSQL 16 padrão  
**Justificativa:** Volume de dados do MVP (2 anos de OHLCV em Parquet, trades pessoais) não justifica a complexidade operacional do TimescaleDB. PostgreSQL com índices em `timestamp DESC` é suficiente. Migrar para TimescaleDB se performance se tornar problema.

### ADR-005: Redis para WebSocket Pub/Sub vs Server-Sent Events

**Decisão:** Redis Pub/Sub + WebSocket  
**Justificativa:** Redis já está na stack para Celery broker. Reutilizá-lo para pub/sub não adiciona dependência nova. WebSocket (vs SSE) é bidirecional — necessário para comandos do frontend ao backend (ex: SUBSCRIBE para símbolos específicos).

### ADR-006: VPS Windows vs Linux para Fase 1 (só cripto)

**Decisão:** Fase 1 pode rodar em Linux Ubuntu 22.04 LTS  
**Justificativa:** Freqtrade + FastAPI + Docker funcionam nativamente em Linux. MT5 (necessário para B3/Forex) requer Windows — mas isso é fase 1b. Começar em Linux é mais barato e mais seguro operacionalmente. Na fase 1b, migrar para VPS Windows ou adicionar Wine.

---

## 10. Plano de Testes

### 10.1 Pirâmide de Testes

```
        /\
       /E2E\          Paper trading (manual, 7 dias)
      /──────\
     /Integr. \        Pytest: API endpoints, ML pipeline, connectors (mock)
    /────────────\
   /  Unit Tests  \    Pytest: risk manager, position sizer, drawdown, features
  /────────────────\
```

### 10.2 Cobertura Mínima por Módulo

| Módulo | Cobertura mínima | Justificativa |
|---|---|---|
| `risk/` | 90% | Dinheiro real — zero tolerância a bugs |
| `trading/order_executor.py` | 90% | Execução de ordens — crítico |
| `ml/feature_engineer.py` | 80% | Feature errada = modelo errado |
| `ml/validator.py` | 80% | Aprovação/rejeição de modelo |
| `api/routes/` | 70% | Contratos da API |
| `notifications/` | 60% | Notificações — importante mas não crítico |

### 10.3 Testes de Integração com Mocks

```python
# Exemplo: test_binance_connector.py
@pytest.fixture
def mock_binance(respx_mock):
    respx_mock.get("https://api.binance.com/api/v3/account").mock(
        return_value=httpx.Response(200, json={"balances": [...]})
    )
    respx_mock.post("https://api.binance.com/api/v3/order/oco").mock(
        return_value=httpx.Response(200, json={"orderId": 12345, ...})
    )
    return respx_mock

async def test_open_position_sends_oco(mock_binance, engine):
    result = await engine.open_position("BTC/USDT", "LONG", 0.00234,
                                         entry=287450, stop=282900, tp=294000)
    assert result.order_id == "12345"
    assert mock_binance.calls[1].request.url.path == "/api/v3/order/oco"
```

---

## 11. Próximos Passos — Roadmap Técnico

### Fase 1 — MVP Cripto (Epics 1–6)

| Epic | Estimativa | Dependências |
|---|---|---|
| 1: Fundação & Infra | 1–2 semanas | — |
| 2: Connector Cripto + Ordens | 2–3 semanas | Epic 1 |
| 3: Gestão de Risco | 1–2 semanas | Epic 2 |
| 4: Pipeline ML | 3–4 semanas | Epic 2, 3 |
| 5: Dashboard Completo | 2–3 semanas | Epics 2, 3, 4 |
| 6: Go-Live + Validação | 1–2 semanas | Epics 1–5 |
| **Total Fase 1** | **10–16 semanas** | — |

### Fase 1b — B3 + Forex (Epic 7)

| Tarefa | Estimativa |
|---|---|
| Setup VPS Windows + MT5 | 1 semana |
| Connector MT5 Python | 1–2 semanas |
| Adaptação pipeline ML (WIN/WDO) | 2 semanas |
| Paper trading + Go-Live | 2 semanas |
| **Total Fase 1b** | **6–7 semanas** |

---

## 12. PO Validation Prompt

> Sarah, temos todos os artefatos do autoxtrade prontos para validação. Por favor, execute o `po-master-checklist` contra os seguintes documentos:
> - `docs/brief.md` — Project Brief (completo)
> - `docs/prd.md` — PRD v1.0 (completo, 7 épicos, 34 FRs, 12 NFRs)
> - `docs/front-end-spec.md` — Front-End Spec v1.0 (7 telas, design tokens, fluxos)
> - `docs/fullstack-architecture.md` — Arquitetura Full-Stack v1.0 (este documento)
>
> Valide consistência entre os artefatos, cobertura de todos os requisitos funcionais e não-funcionais na arquitetura, stories com acceptance criteria testáveis, e riscos não adereçados. Após validação, indique se os artefatos estão prontos para iniciar desenvolvimento ou se há gaps a corrigir.
