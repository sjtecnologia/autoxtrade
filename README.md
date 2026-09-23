# autoxtrade

Robot de trading algorítmico com ML para cripto (Binance), B3 e Forex (MetaTrader 5).

## Visão Geral

- **Fase 1 (Cripto):** CCXT + Binance, Random Forest, paper trading → live
- **Fase 1b (B3/Forex):** MetaTrader 5, modelos por símbolo, mini-contratos
- **Stack:** FastAPI + Celery + PostgreSQL + Redis + Next.js 14

## Pré-Requisitos

- Docker 24+ e Docker Compose v2
- Python 3.11+ (apenas para scripts locais)
- Node.js 20+ (apenas para desenvolvimento frontend)

## Setup Rápido

```bash
# 1. Copiar e preencher variáveis de ambiente
cp .env.example .env
# Editar .env com suas credenciais (Binance API, Telegram, etc.)

# 2. Subir todos os serviços
docker compose up -d

# 3. Verificar status
docker compose ps

# 4. Executar migrações (primeira vez)
docker compose exec backend alembic upgrade head

# 5. Seed dos dados iniciais
docker compose exec backend python scripts/seed_db.py
```

## Estrutura do Repositório

```
autoxtrade/
├── backend/                # Python FastAPI — API, trading engine, ML
│   ├── main.py             # Entrypoint FastAPI
│   ├── config.py           # Settings (pydantic-settings, lê .env)
│   ├── database.py         # Engine SQLAlchemy async + session factory
│   ├── api/                # REST + WebSocket
│   │   ├── auth.py         # Bearer token middleware
│   │   ├── websocket.py    # WebSocket connection manager
│   │   └── routes/         # health, positions, trades, config, risk, ml
│   ├── db/                 # Modelos SQLAlchemy ORM
│   ├── trading/            # Engine de trading
│   │   └── connectors/     # BinanceConnector, MT5Connector, BaseConnector
│   ├── risk/               # PositionSizer, DrawdownMonitor, CorrelationChecker
│   ├── ml/                 # Pipeline ML completo
│   ├── notifications/      # TelegramNotifier
│   ├── tasks/              # Tarefas Celery + Beat schedule
│   ├── alembic/            # Migrações de banco
│   └── tests/              # Testes unitários e de integração
├── frontend/               # Next.js 14 — dashboard
│   ├── app/                # Pages (App Router)
│   ├── components/         # Componentes React reutilizáveis
│   ├── hooks/              # Custom hooks (useWebSocket, usePositions...)
│   ├── store/              # Zustand state management
│   └── lib/                # API client, formatters, constants
├── docs/                   # Documentação
│   ├── prd.md              # Product Requirements Document
│   ├── fullstack-architecture.md
│   └── stories/            # Stories do projeto (SM sharding)
├── scripts/                # Scripts de deploy e manutenção
├── nginx/                  # Configuração Nginx + HTTPS
├── docker-compose.yml      # Orquestração de produção
├── docker-compose.override.yml  # Overrides de desenvolvimento
└── .env.example            # Template de variáveis de ambiente
```

## Documentação

- [Product Requirements Document](docs/prd.md)
- [Arquitetura Full-Stack](docs/fullstack-architecture.md)
- [Front-End Spec](docs/front-end-spec.md)
- [Stories de Desenvolvimento](docs/stories/index.md)

## Comandos Úteis

```bash
# Logs em tempo real
docker compose logs -f backend

# Executar testes
docker compose exec backend pytest tests/ -v

# Acessar banco de dados
docker compose exec db psql -U autoxtrade -d autoxtrade

# Forçar retrain do modelo
docker compose exec celery_worker celery -A tasks.celery_app call tasks.ml_tasks.retrain_models

# Backup do banco
./scripts/backup_db.sh
```
