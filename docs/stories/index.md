# Índice de Stories — autoxtrade

Todas as stories do projeto organizadas por épico. Cada story segue o template padrão com Status, Acceptance Criteria, Tasks e Dev Notes.

## Status Legend

| Status | Descrição |
|---|---|
| Draft | Criada pelo SM, aguardando desenvolvimento |
| In Progress | Dev Agent trabalhando ativamente |
| Review | Implementação concluída, aguardando QA |
| Done | QA aprovado, story concluída |
| Blocked | Bloqueada por dependência ou impedimento |

---

## Epic 0 — Pré-Requisitos do Owner

| Story | Título | Status | Dependências |
|---|---|---|---|
| [0.1](./0.1.prerequisitos-do-owner.md) | Pré-Requisitos do Owner | Draft | — |

---

## Epic 1 — Fundação e Infraestrutura

| Story | Título | Status | Dependências |
|---|---|---|---|
| [1.1](./1.1.setup-repositorio.md) | Setup do Repositório | Draft | 0.1 |
| [1.2](./1.2.containerizacao-docker.md) | Containerização Docker | Draft | 1.1 |
| [1.3](./1.3.banco-de-dados-schema.md) | Banco de Dados e Schema | Draft | 1.2 |
| [1.4](./1.4.api-server-fastapi.md) | API Server FastAPI | Draft | 1.3 |
| [1.5](./1.5.dashboard-minimo.md) | Dashboard Mínimo | Draft | 1.4 |

---

## Epic 2 — Connector Cripto e Engine de Ordens

| Story | Título | Status | Dependências |
|---|---|---|---|
| [2.1](./2.1.connector-binance.md) | Connector Binance (CCXT) | Draft | 1.4 |
| [2.2](./2.2.engine-execucao-ordens.md) | Engine de Execução de Ordens | Draft | 2.1 |
| [2.3](./2.3.notificacoes-telegram.md) | Notificações Telegram | Draft | 1.4 |

---

## Epic 3 — Gerenciamento de Risco

| Story | Título | Status | Dependências |
|---|---|---|---|
| [3.1](./3.1.position-sizing.md) | Position Sizing Dinâmico | Draft | 2.1 |
| [3.2](./3.2.monitor-drawdown.md) | Monitor de Drawdown | Draft | 1.3, 2.3 |
| [3.3](./3.3.correlacao-posicoes.md) | Verificação de Correlação | Draft | 2.1, 3.1 |

---

## Epic 4 — Pipeline de Machine Learning

| Story | Título | Status | Dependências |
|---|---|---|---|
| [4.1](./4.1.coleta-dados-historicos.md) | Coleta de Dados Históricos | Draft | 2.1 |
| [4.2](./4.2.feature-engineering.md) | Engenharia de Features | Draft | 4.1 |
| [4.3](./4.3.treinamento-random-forest.md) | Treinamento Random Forest | Draft | 4.2 |
| [4.4](./4.4.deploy-serving-modelo.md) | Deploy e Serving do Modelo | Draft | 4.3, 1.3 |

---

## Epic 5 — Dashboard Completo

| Story | Título | Status | Dependências |
|---|---|---|---|
| [5.1](./5.1.dashboard-posicoes-tempo-real.md) | Dashboard Principal (Tempo Real) | Draft | 1.5, 2.2, 3.2 |
| [5.2](./5.2.historico-metricas-performance.md) | Histórico e Métricas de Performance | Draft | 5.1 |
| [5.3](./5.3.controles-configuracoes.md) | Controles do Bot e Configurações | Draft | 5.1 |

---

## Epic 6 — Go-Live Cripto (Binance)

| Story | Título | Status | Dependências |
|---|---|---|---|
| [6.1](./6.1.paper-trading-7-dias.md) | Paper Trading 7 Dias | Draft | 4.4, 5.3 |
| [6.2](./6.2.checklist-seguranca-golive.md) | Checklist de Segurança e Go-Live | Draft | 6.1 |

---

## Epic 7 — Integração B3 + Forex (MT5)

| Story | Título | Status | Dependências |
|---|---|---|---|
| [7.1](./7.1.connector-mt5.md) | Connector MetaTrader 5 | Draft | 2.1 (arquitetura base) |
| [7.2](./7.2.pipeline-ml-b3-forex.md) | Pipeline ML para B3 e Forex | Draft | 4.3, 7.1 |
| [7.3](./7.3.paper-trading-mt5-golive.md) | Paper Trading MT5 e Go-Live | Draft | 7.2, 5.3 |

---

## Sumário de Progresso

| Épico | Total | Draft | In Progress | Done |
|---|---|---|---|---|
| Epic 0 | 1 | 1 | 0 | 0 |
| Epic 1 | 5 | 5 | 0 | 0 |
| Epic 2 | 3 | 3 | 0 | 0 |
| Epic 3 | 3 | 3 | 0 | 0 |
| Epic 4 | 4 | 4 | 0 | 0 |
| Epic 5 | 3 | 3 | 0 | 0 |
| Epic 6 | 2 | 2 | 0 | 0 |
| Epic 7 | 3 | 3 | 0 | 0 |
| **Total** | **24** | **24** | **0** | **0** |

---

## Documentos de Referência

| Documento | Descrição |
|---|---|
| [docs/brief.md](../brief.md) | Project Brief — visão e objetivos |
| [docs/prd.md](../prd.md) | Product Requirements Document v1.1 |
| [docs/front-end-spec.md](../front-end-spec.md) | Especificação de Front-End |
| [docs/fullstack-architecture.md](../fullstack-architecture.md) | Arquitetura Full-Stack |
