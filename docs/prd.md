# autoxtrade — Product Requirements Document (PRD)

**Versão:** 1.0  
**Data:** 28 de abril de 2026  
**Status:** Rascunho  
**Owner:** Renato (uso pessoal — MVP)

---

## Change Log

| Data | Versão | Descrição | Autor |
|---|---|---|---|
| 28/04/2026 | 1.0 | Versão inicial criada a partir do Project Brief | PM (John / BMad) |
| 28/04/2026 | 1.1 | Correções pós PO Checklist: Story 0 (pre-requisitos do owner) + AC8-9 Story 2.2 (fallback OCO) | PO (Sarah / BMad) |

---

## 1. Goals and Background Context

### Goals

- Eliminar emoção nas decisões de trading via execução 100% automatizada e baseada em dados
- Operar 24/7 em múltiplos mercados (cripto Fase 1, B3 + Forex Fase 1b) sem intervenção manual
- Superar o desempenho de buy-and-hold com gestão de risco controlada (drawdown ≤ 15%)
- Entregar um sistema com IA/ML adaptativa que aprende e melhora continuamente com dados próprios
- Prover visibilidade completa das operações via dashboard web e notificações Telegram em tempo real
- Estabelecer infraestrutura de backtesting e paper trading para validar estratégias antes de capital real

### Background Context

O trader individual brasileiro enfrenta um dilema crescente: os mercados financeiros operam 24/7 (cripto), exigem reação em milissegundos (B3 futuros) e evoluem de regime constantemente — tornando impossível a operação manual sustentável e lucrativa a longo prazo. As soluções existentes no mercado (3Commas, Cryptohopper, Profit Pro) oferecem automação via regras fixas pré-programadas, mas nenhuma combina múltiplos mercados com aprendizado adaptativo via Machine Learning em uma solução de baixo custo acessível a um trader individual.

O autoxtrade nasce para preencher este gap: um robô de trading pessoal, desenvolvido do zero pelo próprio owner, que usa ML (Random Forest + LSTM) para aprender padrões de mercado, gestão de risco dinâmica baseada em Kelly Criterion, e opera inicialmente em cripto (Binance via Freqtrade) com expansão planejada para B3 e Forex via MetaTrader 5 Python API. O MVP é de uso pessoal exclusivo, validando o sistema antes de qualquer abertura externa.

---

## 2. Requirements

### 2.1 Functional Requirements

**Engine de Trading (Core)**

- FR1: O sistema deve conectar à API da Binance via CCXT/Freqtrade e executar ordens de compra e venda automaticamente com base em sinais do modelo ML.
- FR2: O sistema deve suportar ordens do tipo market, limit, stop-limit e OCO (One Cancels Other).
- FR3: Toda operação aberta DEVE ter stop loss obrigatório configurado na exchange (server-side), não apenas na aplicação.
- FR4: O sistema deve suportar take profit automático, incluindo trailing take profit.
- FR5: O sistema deve executar no máximo N operações simultâneas configuráveis pelo owner (padrão: 3 por mercado).
- FR6: O sistema deve fechar automaticamente todas as posições abertas ao atingir o limite de drawdown diário configurado (padrão: 5% do capital por dia).

**Modelo de Machine Learning**

- FR7: O sistema deve treinar um modelo ML inicial (Random Forest) usando dados históricos OHLCV + indicadores técnicos (RSI, MACD, Bollinger Bands, EMA, Volume) para classificar direção do preço (alta/baixa/neutro).
- FR8: O modelo deve ser retreinado automaticamente em intervalos configuráveis (padrão: semanal) com os dados mais recentes + resultados das operações realizadas.
- FR9: O sistema deve detectar o regime de mercado atual (tendência de alta, tendência de baixa, lateral) e ajustar os parâmetros da estratégia conforme o regime.
- FR10: Antes de qualquer novo modelo ser ativado em produção, ele deve ser validado com backtesting walk-forward out-of-sample com performance mínima de Profit Factor ≥ 1.3.
- FR11: O sistema deve manter histórico de versões dos modelos treinados, permitindo rollback ao modelo anterior.

**Gestão de Risco**

- FR12: O sistema deve calcular o tamanho de posição dinamicamente usando position sizing baseado em risco percentual configurável (padrão: 1% do capital por operação).
- FR13: O sistema deve monitorar e limitar o drawdown: alertar ao atingir 10% e pausar automaticamente o trading ao atingir 15% do capital total em qualquer janela de 30 dias.
- FR14: O sistema deve calcular e exibir métricas de risco em tempo real: Win Rate, Profit Factor, Sharpe Ratio, Max Drawdown, R:R médio.
- FR15: O sistema não deve abrir nova posição se a correlação com uma posição já aberta for superior a 0.7 (evitar exposição duplicada ao mesmo risco).

**Backtesting**

- FR16: O sistema deve oferecer módulo de backtesting com dados históricos OHLCV (mínimo 2 anos) para qualquer estratégia antes de ativação.
- FR17: O backtesting deve calcular e exibir: Win Rate, Profit Factor, Sharpe Ratio, Max Drawdown, número de trades, retorno total.
- FR18: O sistema deve suportar backtesting walk-forward (treinar em período A, testar em período B) para evitar overfitting.

**Paper Trading**

- FR19: O sistema deve oferecer modo paper trading que simula execução de ordens com dados de mercado reais sem enviar ordens à exchange.
- FR20: O paper trading deve usar as mesmas métricas e lógica do trading real para validação fiel da estratégia.
- FR21: Qualquer estratégia ou modelo novo deve passar por mínimo 7 dias de paper trading com performance aceitável antes de ser promovido a produção.

**Dashboard Web**

- FR22: O dashboard deve exibir status em tempo real de todas as posições abertas: ativo, lado (long/short), preço de entrada, preço atual, P&L em % e R$.
- FR23: O dashboard deve exibir histórico completo de operações com filtros por data, mercado, ativo e resultado.
- FR24: O dashboard deve exibir métricas de performance consolidadas e por mercado: P&L total, Win Rate, Profit Factor, Sharpe Ratio, Max Drawdown do período.
- FR25: O dashboard deve exibir status do sistema: modelo ML ativo (versão, data de treino, performance no backtesting), estado do bot (ativo/pausado/paper trading), uptime.
- FR26: O owner deve poder pausar e retomar o bot por mercado individualmente via dashboard.
- FR27: O dashboard deve exibir gráfico de equity curve (evolução do capital ao longo do tempo).

**Notificações**

- FR28: O sistema deve enviar notificação via Telegram a cada operação aberta com: ativo, lado, preço de entrada, tamanho, stop loss, take profit.
- FR29: O sistema deve enviar notificação via Telegram a cada operação fechada com: resultado em % e R$, duração da operação, motivo de fechamento (TP/SL/manual/drawdown).
- FR30: O sistema deve enviar alerta via Telegram ao atingir 10% de drawdown (aviso) e 15% de drawdown (operações pausadas automaticamente).
- FR31: O sistema deve enviar relatório diário via Telegram às 23h com resumo do dia: operações realizadas, P&L do dia, P&L acumulado, drawdown atual.

**Integração MT5 — B3 + Forex (Fase 1b)**

- FR32: O sistema deve conectar ao MetaTrader 5 via Python API (`MetaTrader5` lib) para envio de ordens nos mercados B3 Futuros (WIN, WDO) e Forex (principais pares).
- FR33: O sistema deve respeitar os horários de funcionamento da B3 (09h00–17h55 BRT) e não enviar ordens fora deste horário.
- FR34: O sistema deve calcular e exibir custos de transação estimados (corretagem, emolumentos B3) antes de abrir posição.

---

### 2.2 Non-Functional Requirements

- NFR1: O sistema deve ter uptime ≥ 99,5% medido mensalmente — tolerância de no máximo 3,6 horas de inatividade por mês.
- NFR2: A latência entre geração do sinal de trading e envio da ordem à exchange deve ser ≤ 500ms em condições normais de rede.
- NFR3: O sistema deve reiniciar automaticamente em caso de falha (crash do processo) em até 30 segundos, usando supervisor de processos (systemd ou supervisor).
- NFR4: API keys das exchanges e credenciais devem ser armazenadas criptografadas (AES-256) no servidor — NUNCA em texto plano ou versionadas no repositório.
- NFR5: As API keys configuradas nas exchanges devem ter permissão SOMENTE de trade (sem permissão de saque/withdrawal).
- NFR6: O servidor VPS deve ter firewall configurado para aceitar conexões externas apenas nas portas necessárias (dashboard HTTPS, SSH com chave).
- NFR7: O dashboard web deve ser responsivo e funcionar nos navegadores Chrome e Firefox (últimas 2 versões major).
- NFR8: O sistema deve logar todas as operações realizadas, erros e eventos críticos em arquivo de log com retenção mínima de 90 dias.
- NFR9: O banco de dados deve ter backup automático diário com retenção de 30 dias.
- NFR10: O custo total de infraestrutura (VPS + banco de dados) deve ser ≤ R$ 200/mês.
- NFR11: O sistema deve processar dados de mercado em tempo real via WebSocket (sem polling) para garantir latência mínima.
- NFR12: O modelo ML não deve consumir mais de 4GB de RAM durante inferência para caber em VPS de baixo custo.

---

## 3. User Interface Design Goals

### 3.1 Overall UX Vision

Interface funcional, densa em informação e voltada para o trader técnico — sem elementos decorativos desnecessários. Inspiração: terminais profissionais de trading (Bloomberg light, TradingView). Fundo escuro (dark mode nativo) para uso prolongado sem fadiga visual. Dados em tempo real com atualização automática sem reload de página.

### 3.2 Key Interaction Paradigms

- **Read-heavy com ações rápidas:** O owner passa 90% do tempo visualizando dados; ações (pausar/retomar bot) são secundárias mas devem ser acessíveis com 1 clique.
- **Real-time first:** Qualquer dado de mercado ou posição aberta deve atualizar automaticamente via WebSocket — sem botão de "refresh".
- **Alertas proativos:** O sistema informa o owner sobre eventos importantes (drawdown, operações, anomalias) sem necessidade de monitoramento ativo.

### 3.3 Core Screens and Views

1. **Dashboard Principal** — Status geral: posições abertas, P&L do dia, equity curve, status do bot e modelo ML
2. **Posições Abertas** — Tabela com todas as posições em tempo real: ativo, lado, entrada, atual, P&L, SL, TP
3. **Histórico de Operações** — Tabela paginada com filtros; detalhes de cada trade fechado
4. **Performance & Métricas** — Win Rate, Profit Factor, Sharpe, Drawdown por período e por mercado; gráficos de equity curve
5. **Gerenciamento de Bots** — Status por mercado (cripto/B3/Forex); botões pausar/retomar; modelo ML ativo
6. **Backtesting** — Interface para rodar backtests: selecionar período, estratégia, visualizar resultados
7. **Configurações** — Parâmetros de risco (% por trade, drawdown máximo), configuração de notificações

### 3.4 Accessibility

WCAG AA básico — contraste adequado, navegação por teclado nas ações críticas. Dashboard é uso pessoal; não há requisito de acessibilidade avançada para MVP.

### 3.5 Branding

- **Dark mode** nativo (fundo: `#0d1117` estilo GitHub Dark)
- **Cores de status:** Verde para lucro/long, Vermelho para perda/short, Amarelo para alertas, Azul para neutro
- **Tipografia:** Monospace para valores numéricos (preços, P&L); sans-serif para texto geral
- Nome: **autoxtrade** — logo simples, texto com ícone de gráfico estilizado

### 3.6 Target Device and Platforms

Web Responsivo — desktop first (uso principal em monitor), mas funcional em tablet para consulta rápida. Não é necessário mobile app nativo para MVP.

---

## 4. Technical Assumptions

### 4.1 Repository Structure

**Monorepo** — backend Python + frontend Next.js no mesmo repositório Git. Estrutura:

```
autoxtrade/
├── backend/          # FastAPI + engine de trading + ML
│   ├── trading/      # engine, connectors, risk management
│   ├── ml/           # modelos, treinamento, backtesting
│   ├── api/          # endpoints REST + WebSocket
│   └── notifications/ # Telegram bot
├── frontend/         # Next.js dashboard
├── docs/             # PRD, arquitetura, stories
├── scripts/          # scripts de deploy, manutenção
└── docker-compose.yml
```

### 4.2 Service Architecture

**Monolith modular** com separação lógica clara — não microsserviços para MVP (complexidade operacional desnecessária para uso pessoal). Componentes:

- **Trading Engine** (Python): Freqtrade como framework base para cripto; MT5 Python API para B3/Forex na fase 1b
- **ML Service** (Python): scikit-learn (Random Forest) + TensorFlow/Keras (LSTM); treinamento assíncrono via Celery
- **API Server** (FastAPI): REST + WebSocket para comunicação com o dashboard
- **Task Scheduler** (Celery + Redis): retreinamento do modelo, relatórios diários, manutenção
- **Dashboard** (Next.js + TailwindCSS): SPA com WebSocket para dados em tempo real
- **Database** (PostgreSQL): persistência de operações, modelos, configurações
- **Cache** (Redis): dados de mercado em tempo real, filas de tarefas

### 4.3 Testing Requirements

**Unit + Integration** com foco em componentes críticos:

- Unit tests obrigatórios para: engine de risk management, cálculo de position sizing, lógica de stop loss/take profit, validação de ordens
- Integration tests para: conectores de exchange (com mocks), API endpoints, pipeline ML (treino → validação → deploy)
- E2E manual: paper trading como teste de ponta a ponta antes de go-live
- Cobertura mínima: 70% nos módulos de risk management e trading engine
- Framework: `pytest` + `pytest-asyncio` para testes assíncronos

### 4.4 Additional Technical Assumptions

- **Python 3.11+** como versão mínima (melhor performance e tipagem)
- **Freqtrade** como framework base para trading cripto (open-source, ML integrado, backtesting nativo) — customizado para integração com ML pipeline próprio
- **MetaTrader 5 Python API** (`pip install MetaTrader5`) para B3 e Forex — requer conta ativa em XP Investimentos ou Modal Mais (gratuito)
- **VPS Windows** (ou Linux com Wine) necessário para MT5 na fase 1b; fase 1 (só cripto) pode rodar em Linux puro
- **Docker + Docker Compose** para containerização de todos os serviços — facilita deploy e manutenção
- **Nginx** como reverse proxy para o dashboard com HTTPS (Let's Encrypt)
- **Variáveis de ambiente + python-dotenv** para gerenciamento seguro de credenciais; nunca versionadas no git
- **Git + GitHub** para versionamento; branch `main` = produção; branch `develop` = desenvolvimento
- Dados históricos para backtesting: Freqtrade download nativo via exchange API (Binance oferece até 3 anos de OHLCV gratuito)
- Dados históricos B3: Yahoo Finance (`yfinance`) ou MT5 histórico interno para fase 1b

---

## 5. Epic List

| # | Título | Objetivo |
|---|---|---|
| 0 | **Pre-requisitos do Owner** | Tarefa humana: contas externas, VPS, chaves de API, Telegram bot — obrigatório antes do Epic 1 |
| 1 | **Fundação & Infraestrutura** | Setup completo do projeto, containerização, CI básico, dashboard estrutural mínimo funcionando |
| 2 | **Connector Cripto + Engine de Ordens** | Integração Binance via Freqtrade, execução de ordens reais/paper, stop loss server-side + fallback OCO, notificações Telegram |
| 3 | **Gestão de Risco & Position Sizing** | Motor de risco completo: position sizing dinâmico, limites de drawdown, correlação, pausas automáticas |
| 4 | **Pipeline ML — Modelo Inicial** | Coleta de dados históricos, features de indicadores, treino Random Forest, backtesting walk-forward, validação e deploy |
| 5 | **Dashboard Completo & Monitoramento** | Interface completa com posições em tempo real, histórico, equity curve, métricas de performance, controles do bot |
| 6 | **Go-Live Cripto & Validação** | Paper trading 7 dias, ajustes finais, ativação em conta real com capital mínimo, monitoramento |
| 7 | **Integração B3 + Forex (MT5)** | Connector MT5 Python, adaptação do pipeline ML para B3/Forex, testes, paper trading e go-live |

---

## 6. Epic Details

---

### Story 0 — Checklist de Pre-requisitos do Owner

**Tipo:** Tarefa do owner (humano) — deve ser concluída ANTES de iniciar o Epic 1

Como owner do autoxtrade,
Quero garantir que todos os pre-requisitos externos estão configurados,
Para que o desenvolvimento possa iniciar sem bloqueios nas semanas seguintes.

**Pre-requisitos obrigatórios — marcar cada item antes de iniciar Epic 1:**

**Infraestrutura:**
- [ ] VPS provisionado (Ubuntu 22.04 LTS, mínimo 4GB RAM, 40GB SSD, região Brasil ou Europa) — ~$15–25/mês
- [ ] Domínio registrado ou subdomínio configurado apontando para o IP do VPS (ex: `autoxtrade.meudominio.com`)
- [ ] Acesso SSH com chave configurado no VPS; login por senha desabilitado
- [ ] Docker + Docker Compose instalados no VPS (rodar `scripts/setup_vps.sh`)

**Binance (Exchange Cripto):**
- [ ] Conta Binance ativa com verificação de identidade (KYC) concluída
- [ ] API Key criada em `Binance > Perfil > API Management` com permissões: **Enable Reading** + **Enable Spot & Margin Trading** — **NÃO habilitar Enable Withdrawals**
- [ ] IP Whitelist configurado na API Key com o IP fixo do VPS
- [ ] API Key e Secret copiados e armazenados com segurança (serão inseridos no `.env`)

**Telegram (Notificações):**
- [ ] Bot Telegram criado via `@BotFather` → `/newbot` → copiar o token gerado
- [ ] Chat ID obtido: enviar mensagem para o bot e acessar `https://api.telegram.org/bot<TOKEN>/getUpdates` para obter o `chat_id`
- [ ] Token e Chat ID copiados para inserção no `.env`

**MetaTrader 5 — Fase 1b (pode ser adiado para Epic 7):**
- [ ] Conta aberta em XP Investimentos ou Modal Mais (gratuito, CPF necessário)
- [ ] MetaTrader 5 instalado e autenticado com a conta da corretora
- [ ] Login, senha e nome do servidor MT5 anotados para uso no `.env`

**Repositório:**
- [ ] Repositório GitHub criado (privado) para o projeto
- [ ] Chave SSH do VPS adicionada ao GitHub para deploy automatizado

---

### Epic 1: Fundação & Infraestrutura

**Objetivo:** Estabelecer a base técnica completa do projeto — repositório, containerização, banco de dados, estrutura de configurações seguras e um dashboard mínimo funcional (health check + tela de login). Ao final deste epic, o sistema deve estar rodando no VPS com todos os serviços levantados via Docker Compose e o owner consegue acessar o dashboard via HTTPS.

---

#### Story 1.1 — Setup do Repositório e Estrutura do Projeto

Como owner do autoxtrade,
Quero o repositório Git criado com a estrutura de pastas definida, .gitignore, e documentação inicial,
Para que a base do projeto esteja organizada e pronta para desenvolvimento.

**Acceptance Criteria:**
1. Repositório Git inicializado com branches `main` e `develop`
2. Estrutura de pastas criada: `backend/`, `frontend/`, `docs/`, `scripts/`
3. `.gitignore` configurado para Python, Node.js e arquivos de ambiente (`.env`, `*.pem`, `*.key`)
4. `README.md` com visão geral do projeto e instruções básicas de setup
5. `requirements.txt` (Python) e `package.json` (Next.js) com dependências iniciais definidas

---

#### Story 1.2 — Containerização com Docker Compose

Como owner do autoxtrade,
Quero todos os serviços (backend, frontend, PostgreSQL, Redis) containerizados e orquestrados via Docker Compose,
Para que o ambiente seja reproduzível e o deploy seja simples com um único comando.

**Acceptance Criteria:**
1. `docker-compose.yml` define serviços: `backend`, `frontend`, `db` (PostgreSQL), `redis`
2. `Dockerfile` para backend Python e frontend Next.js criados e funcionais
3. Variáveis de ambiente carregadas de arquivo `.env` (não versionado); `.env.example` documentado no repositório
4. Comando `docker compose up -d` levanta todos os serviços sem erros
5. Healthcheck configurado para cada serviço; `docker compose ps` mostra todos `healthy`
6. Volumes persistentes configurados para PostgreSQL e Redis

---

#### Story 1.3 — Banco de Dados: Schema Inicial e Migrações

Como sistema autoxtrade,
Quero o schema inicial do banco de dados criado com suporte a migrações,
Para que os dados de operações, modelos e configurações sejam persistidos de forma estruturada e evolutiva.

**Acceptance Criteria:**
1. Alembic configurado para gerenciamento de migrações PostgreSQL
2. Tabelas iniciais criadas: `trades` (operações), `models` (versões ML), `bot_config` (configurações)
3. Schema da tabela `trades` inclui: id, market, symbol, side, entry_price, exit_price, quantity, pnl, pnl_pct, status, open_at, close_at, close_reason, model_version
4. Schema da tabela `bot_config` inclui: market, is_active, max_open_trades, risk_per_trade_pct, max_drawdown_pct, last_updated
5. Migração inicial executada com sucesso; `alembic current` mostra revisão aplicada
6. Seeds com configuração padrão inseridos na tabela `bot_config`

---

#### Story 1.4 — API Server Base (FastAPI) + WebSocket

Como dashboard frontend,
Quero uma API REST funcional com suporte a WebSocket,
Para que o frontend possa buscar dados e receber atualizações em tempo real.

**Acceptance Criteria:**
1. FastAPI inicializado com estrutura de routers modulares (`/api/v1/`)
2. Endpoint `GET /health` retorna status `{"status": "ok", "timestamp": "..."}` com HTTP 200
3. Endpoint `GET /api/v1/config` retorna configuração atual do bot
4. Endpoint `POST /api/v1/config` atualiza configuração (bot ativo/pausado por mercado)
5. WebSocket endpoint `WS /ws` estabelece conexão e envia `ping` a cada 30 segundos
6. Autenticação básica via token estático configurado em variável de ambiente (sem multi-usuário)
7. Testes unitários para todos os endpoints com pytest — cobertura ≥ 80%

---

#### Story 1.5 — Dashboard Mínimo (Next.js) com Health Status

Como owner do autoxtrade,
Quero um dashboard web acessível via HTTPS no VPS com tela inicial mostrando status de saúde do sistema,
Para que eu possa verificar se todos os serviços estão rodando corretamente.

**Acceptance Criteria:**
1. Projeto Next.js inicializado com TailwindCSS, dark mode configurado por padrão
2. Tela de status exibe: status de cada serviço (backend, database, redis) com indicador verde/vermelho
3. Tela exibe versão do sistema e timestamp da última atualização
4. Nginx configurado como reverse proxy com HTTPS (Let's Encrypt) apontando para o VPS
5. Dashboard acessível via browser em `https://[domínio-ou-IP]` sem erros SSL
6. Conexão WebSocket estabelecida e exibindo indicador de conexão ativa no dashboard

---

### Epic 2: Connector Cripto + Engine de Ordens

**Objetivo:** Implementar a integração completa com a Binance via Freqtrade (ou CCXT), executar ordens reais e em modo paper trading com stop loss server-side obrigatório, e enviar notificações via Telegram a cada evento de operação. Ao final deste epic, o sistema consegue abrir e fechar posições na Binance com segurança.

---

#### Story 2.1 — Connector Binance via CCXT/Freqtrade

Como engine de trading,
Quero conectar à API da Binance de forma autenticada e buscar dados de mercado em tempo real,
Para que o sistema tenha acesso a preços, orderbooks e possa executar ordens.

**Acceptance Criteria:**
1. Credenciais da Binance (API key + secret) carregadas de variável de ambiente — nunca hardcoded
2. Conexão estabelecida com sucesso: `GET /api/v3/account` retorna dados da conta sem erro
3. Sistema busca OHLCV em tempo real via WebSocket para pares configurados (ex: BTC/USDT, ETH/USDT)
4. Sistema busca saldo disponível por ativo e expõe via endpoint `GET /api/v1/balance`
5. Limite de rate da API da Binance respeitado; sistema implementa backoff exponencial em caso de `429`
6. Modo paper trading configurável: quando ativo, ordens são simuladas localmente sem chamar API de ordens da Binance
7. Testes de integração com mocks da API da Binance passando

---

#### Story 2.2 — Engine de Execução de Ordens

Como engine de trading,
Quero enviar ordens de compra e venda com stop loss obrigatório diretamente na exchange,
Para que cada posição aberta tenha proteção de risco garantida mesmo em caso de falha do servidor.

**Acceptance Criteria:**
1. Suporte a ordens: market, limit e OCO (stop loss + take profit simultâneos)
2. Toda ordem de compra/abertura de posição DEVE ser acompanhada de ordem OCO de stop loss na exchange (server-side) — sistema rejeita abertura sem stop loss configurado
3. Sistema persiste todas as ordens enviadas na tabela `trades` com status `open`
4. Sistema monitora status das ordens via WebSocket e atualiza `trades` quando ordem é executada ou cancelada
5. Em modo paper trading, ordens são simuladas: stop loss e take profit disparam quando preço cruza os níveis simulados
6. Endpoint `GET /api/v1/positions` retorna posições abertas com dados em tempo real
7. Testes unitários para lógica de construção de ordens OCO com 100% de cobertura
8. **Fallback de falha OCO:** Se o envio da ordem OCO falhar (erro da exchange, timeout ou rejeição), o sistema deve: (a) tentar enviar ordens separadas de stop-market como fallback imediato; (b) se o fallback também falhar, fechar a posição a mercado (`order_type=market`) imediatamente; (c) registrar o evento na tabela `trade_events` com tipo `OCO_FAILED`; (d) enviar notificação Telegram de emergência com o erro e ação tomada
9. **Monitoramento de posições órfãs:** Ao inicializar o sistema, verificar se existem posições abertas na exchange sem OCO correspondente — para cada uma encontrada, recriar o OCO ou fechar a posição a mercado e notificar

---

#### Story 2.3 — Notificações Telegram

Como owner do autoxtrade,
Quero receber notificações via Telegram a cada evento importante do bot,
Para que eu esteja informado sobre operações e alertas sem precisar abrir o dashboard.

**Acceptance Criteria:**
1. Bot Telegram configurado (token via variável de ambiente); `sendMessage` funcional
2. Notificação enviada ao abrir posição: símbolo, lado (LONG/SHORT), preço entrada, quantidade, stop loss, take profit
3. Notificação enviada ao fechar posição: símbolo, lado, resultado em R$ e %, duração, motivo (TP hit / SL hit / manual / drawdown limit)
4. Notificação enviada em caso de erro crítico da API da exchange (conexão perdida, ordem rejeitada)
5. Notificação com relatório diário às 23h BRT: operações do dia, P&L do dia, P&L acumulado
6. Fila de notificações com retry: se envio falhar, tenta novamente até 3 vezes com backoff
7. Testes unitários para formatação das mensagens Telegram

---

### Epic 3: Gestão de Risco & Position Sizing

**Objetivo:** Implementar o motor de risco completo do autoxtrade — cálculo dinâmico de tamanho de posição, monitoramento e aplicação de limites de drawdown, detecção de correlação entre posições e pausas automáticas. Ao final deste epic, o sistema jamais abre uma posição que viole as regras de risco configuradas.

---

#### Story 3.1 — Position Sizing Dinâmico

Como engine de trading,
Quero calcular automaticamente o tamanho de cada posição com base no capital disponível e no risco percentual configurado,
Para que nenhuma operação individual coloque mais do que o percentual definido do capital em risco.

**Acceptance Criteria:**
1. Fórmula implementada: `quantidade = (capital × risco_pct) / distância_stop_loss`
2. Parâmetro `risk_per_trade_pct` configurável via dashboard e API (padrão: 1%)
3. Sistema busca capital atual disponível antes de cada operação
4. Sistema recusa abertura de posição se o tamanho calculado exceder 10% do capital total (proteção extra)
5. Tamanho calculado é arredondado para o step size mínimo do par na exchange (ex: 0.001 BTC)
6. Endpoint `GET /api/v1/risk/sizing?symbol=BTC/USDT&stop_pct=1.5` retorna tamanho sugerido para simulação
7. Testes unitários com 100% de cobertura para função de cálculo de position size

---

#### Story 3.2 — Monitor de Drawdown e Pausas Automáticas

Como owner do autoxtrade,
Quero que o sistema monitore continuamente o drawdown e pause automaticamente as operações ao atingir limites configurados,
Para que o capital seja protegido em períodos adversos de mercado.

**Acceptance Criteria:**
1. Sistema calcula drawdown atual em tempo real: `(pico_capital - capital_atual) / pico_capital × 100`
2. Drawdown calculado em janelas de 1 dia e 30 dias
3. Ao atingir 10% de drawdown (30 dias): notificação Telegram de alerta; dashboard exibe banner de aviso amarelo
4. Ao atingir 15% de drawdown (30 dias): todas as novas ordens são bloqueadas automaticamente; notificação Telegram de emergência; dashboard exibe banner vermelho "TRADING PAUSADO"
5. Owner pode retomar trading manualmente via dashboard após reconhecer o alerta, mesmo com drawdown > 15%
6. Endpoint `GET /api/v1/risk/drawdown` retorna drawdown atual (1d e 30d) e status (ok/warning/paused)
7. Testes unitários para lógica de cálculo de drawdown e trigger de pausas

---

#### Story 3.3 — Verificação de Correlação entre Posições

Como engine de trading,
Quero que o sistema verifique a correlação entre a nova posição candidata e as posições já abertas,
Para que o portfólio não fique excessivamente exposto a um único fator de risco.

**Acceptance Criteria:**
1. Sistema calcula correlação de Pearson entre retornos dos últimos 20 candles do ativo candidato e de cada posição aberta
2. Sistema recusa abertura de nova posição se correlação com qualquer posição aberta for ≥ 0.7 (configurável)
3. Em modo paper trading, correlação é calculada mas não bloqueia — apenas registra em log e emite aviso
4. Log registra decisão de correlação para cada tentativa de abertura de posição
5. Testes unitários para função de cálculo de correlação e lógica de bloqueio

---

### Epic 4: Pipeline ML — Modelo Inicial

**Objetivo:** Implementar o pipeline completo de Machine Learning: coleta e processamento de dados históricos, engenharia de features (indicadores técnicos), treinamento do modelo Random Forest, backtesting walk-forward para validação, e deploy automatizado do modelo em produção com rollback seguro.

---

#### Story 4.1 — Coleta e Processamento de Dados Históricos

Como pipeline ML,
Quero coletar e armazenar dados históricos OHLCV da Binance com pelo menos 2 anos de histórico,
Para que o modelo ML tenha dados suficientes para treinamento e validação confiáveis.

**Acceptance Criteria:**
1. Script de download de dados históricos OHLCV via Freqtrade (`freqtrade download-data`) para pares configurados (BTC/USDT, ETH/USDT)
2. Dados baixados para mínimo 2 anos em timeframes de 5m, 1h e 1d
3. Dados armazenados em formato Parquet (comprimido) na pasta `backend/ml/data/`
4. Script de validação de qualidade dos dados: detecta e remove candles duplicados, nulos e gaps > 3 candles consecutivos
5. Pipeline de dados incrementais: script de atualização diária baixa apenas candles novos desde o último download
6. Testes unitários para funções de validação e limpeza de dados

---

#### Story 4.2 — Engenharia de Features e Indicadores Técnicos

Como modelo ML,
Quero calcular features baseadas em indicadores técnicos a partir dos dados OHLCV,
Para que o modelo tenha sinais relevantes para classificar a direção do preço.

**Acceptance Criteria:**
1. Features implementadas: RSI(14), MACD(12,26,9), Bollinger Bands(20,2), EMA(9,21,50,200), ATR(14), OBV (On-Balance Volume), Stochastic(14,3)
2. Features de regime de mercado: ADX(14) para força de tendência, variação de volume (volume atual vs média 20 períodos)
3. Target (label) calculado: retorno futuro em N candles; binarizado em LONG (retorno > +0.5%), SHORT (retorno < -0.5%), NEUTRO (entre -0.5% e +0.5%)
4. Features normalizadas com StandardScaler; scaler salvo junto ao modelo para uso em inferência
5. Pipeline de features reproduzível: mesma entrada → mesma saída, sem data leakage
6. Testes unitários para cada indicador com valores conhecidos

---

#### Story 4.3 — Treinamento do Modelo Random Forest e Backtesting Walk-Forward

Como sistema autoxtrade,
Quero treinar um modelo Random Forest e validá-lo via backtesting walk-forward out-of-sample,
Para que apenas modelos com performance comprovada sejam ativados em produção.

**Acceptance Criteria:**
1. Modelo Random Forest treinado com scikit-learn; hiperparâmetros otimizados via GridSearchCV em dados de treino
2. Validação walk-forward implementada: janelas deslizantes de treino (12 meses) + teste (3 meses) em pelo menos 4 folds
3. Métricas calculadas em cada fold: Accuracy, Precision, Recall por classe (LONG/SHORT/NEUTRO)
4. Métricas de trading simuladas: Profit Factor, Sharpe Ratio, Max Drawdown, Win Rate
5. Modelo aprovado para produção SOMENTE se Profit Factor médio nos folds de teste ≥ 1.3
6. Modelo rejeitado (Profit Factor < 1.3) gera relatório de rejeição e mantém modelo anterior ativo
7. Modelo aprovado salvo em `backend/ml/models/` com versão timestampada (ex: `rf_v20260428_143022`)
8. Testes unitários para pipeline de treinamento e validação de thresholds

---

#### Story 4.4 — Deploy e Serving do Modelo em Produção

Como engine de trading,
Quero que o modelo ML aprovado seja carregado automaticamente e sirva predições em tempo real,
Para que as decisões de trading sejam baseadas nas predições mais recentes do modelo.

**Acceptance Criteria:**
1. Endpoint `POST /api/v1/ml/predict` recebe candles OHLCV recentes e retorna predição: `{"signal": "LONG"|"SHORT"|"NEUTRO", "confidence": 0.0-1.0, "model_version": "..."}`
2. Predições com `confidence < 0.6` são tratadas como NEUTRO (sem operação) — limiar configurável
3. Tabela `models` no banco registra cada modelo: versão, data treino, métricas de backtesting, status (active/inactive/rejected)
4. Apenas 1 modelo com status `active` por vez; deploy de novo modelo automaticamente marca anterior como `inactive`
5. Endpoint `POST /api/v1/ml/rollback` permite reverter para o modelo `inactive` anterior
6. Dashboard exibe: modelo ativo, versão, data de treino, Profit Factor do backtesting
7. Retreinamento automático agendado semanalmente via Celery beat

---

### Epic 5: Dashboard Completo & Monitoramento

**Objetivo:** Implementar a interface web completa com todas as telas definidas no PRD: posições em tempo real, histórico de operações, equity curve, métricas de performance e controles do bot. Ao final deste epic, o owner tem visibilidade total do sistema sem precisar acessar logs ou terminal.

---

#### Story 5.1 — Dashboard Principal com Posições em Tempo Real

Como owner do autoxtrade,
Quero visualizar todas as posições abertas e métricas principais em tempo real no dashboard,
Para que eu tenha visibilidade instantânea do estado atual do portfólio.

**Acceptance Criteria:**
1. Tabela de posições abertas atualiza via WebSocket a cada novo candle: símbolo, lado, entrada, preço atual, P&L R$ e %, tempo aberto, SL, TP
2. Métricas do dia exibidas no topo: P&L do dia (R$ e %), número de operações, Win Rate do dia
3. Indicadores de status: bot ativo/pausado por mercado (verde/vermelho), modelo ML ativo e versão
4. Equity curve do mês atual exibida como sparkline no header
5. Drawdown atual exibido com barra de progresso colorida (verde < 5%, amarelo 5–10%, vermelho > 10%)
6. Conexão WebSocket exibida; reconecta automaticamente em caso de queda

---

#### Story 5.2 — Histórico de Operações e Métricas de Performance

Como owner do autoxtrade,
Quero visualizar o histórico completo de operações com filtros e métricas de performance agregadas,
Para que eu possa analisar o desempenho do bot por período e por mercado.

**Acceptance Criteria:**
1. Tabela de histórico com paginação (50 por página): data, símbolo, lado, entrada, saída, P&L R$, P&L %, duração, motivo fechamento
2. Filtros funcionais: por período (hoje/semana/mês/customizado), por mercado, por resultado (lucro/perda)
3. Painel de métricas agregadas calculadas a partir dos filtros ativos: Win Rate, Profit Factor, Sharpe Ratio, Total P&L, Max Drawdown, Número de Trades, Média de P&L por trade
4. Equity curve interativa (Recharts) mostrando evolução do capital no período filtrado
5. Export CSV do histórico filtrado disponível via botão
6. Testes E2E manuais validam cálculo das métricas contra dados conhecidos

---

#### Story 5.3 — Controles do Bot e Página de Configurações

Como owner do autoxtrade,
Quero controlar o bot (pausar/retomar por mercado) e ajustar parâmetros de risco via dashboard,
Para que eu possa gerenciar o sistema sem precisar editar arquivos de configuração ou reiniciar serviços.

**Acceptance Criteria:**
1. Botões Pausar/Retomar por mercado com confirmação modal antes de executar ação
2. Formulário de configuração de risco: `risk_per_trade_pct` (0.1%–5%), `max_drawdown_pct` (5%–30%), `max_open_trades` (1–10) por mercado
3. Validação client-side e server-side dos parâmetros antes de salvar
4. Alterações de configuração registradas em log com timestamp e valor anterior/novo
5. Notificação Telegram enviada quando bot é pausado/retomado manualmente
6. Testes unitários para validação dos parâmetros de configuração

---

### Epic 6: Go-Live Cripto & Validação

**Objetivo:** Executar 7 dias de paper trading com o modelo ML ativo, validar métricas, corrigir issues encontrados e ativar o bot em conta real com capital mínimo de segurança. Ao final deste epic, o autoxtrade está operando autonomamente com dinheiro real pela primeira vez.

---

#### Story 6.1 — Paper Trading 7 Dias com Monitoramento

Como owner do autoxtrade,
Quero executar o bot em modo paper trading por 7 dias corridos com monitoramento ativo,
Para que todos os componentes sejam validados em condições de mercado reais antes de capital real.

**Acceptance Criteria:**
1. Bot em modo paper trading ativo por 7 dias consecutivos sem interrupções técnicas (uptime ≥ 99%)
2. Mínimo de 10 operações simuladas registradas durante o período
3. Todas as notificações Telegram disparadas corretamente para cada evento
4. Dashboard exibe dados corretos e atualizados em tempo real durante todo o período
5. Log de erros auditado: zero erros críticos; erros menores documentados e corrigidos
6. Relatório final de paper trading: P&L simulado, Win Rate, Profit Factor, observações de comportamento anômalo

---

#### Story 6.2 — Checklist de Segurança e Go-Live com Capital Real

Como owner do autoxtrade,
Quero verificar todos os itens de segurança e ativar o bot com capital real mínimo,
Para que a transição de paper para produção seja segura e controlada.

**Acceptance Criteria:**
1. Checklist pré-go-live concluído: API keys com permissão somente trade (sem saque) verificadas, stop loss server-side testado manualmente, drawdown automático testado forçando perda, VPS com firewall configurado verificado, backup do banco funcionando
2. Capital inicial configurado para no máximo R$ 500 (ou equivalente em cripto) na conta de trading
3. Bot ativado em produção com 1 par de trading (BTC/USDT) como experimento inicial
4. Primeira operação real registrada no banco e notificação Telegram recebida com sucesso
5. Monitoramento intensivo nas primeiras 24 horas: verificar a cada 2 horas que o bot está operando corretamente
6. Documentação de go-live: data, capital inicial, configurações ativas, modelo ML versão

---

### Epic 7: Integração B3 + Forex (MT5)

**Objetivo:** Implementar o connector MetaTrader 5 via Python API para operar nos mercados brasileiros (WIN, WDO) e Forex, adaptar o pipeline ML para os novos mercados, validar via paper trading e ativar em produção. Ao final deste epic, o autoxtrade opera nos três mercados planejados.

---

#### Story 7.1 — Connector MetaTrader 5 Python API

Como engine de trading,
Quero conectar ao MetaTrader 5 via Python API e executar ordens nos mercados B3 Futuros e Forex,
Para que o autoxtrade amplie sua atuação além do mercado cripto.

**Acceptance Criteria:**
1. Biblioteca `MetaTrader5` instalada e conexão estabelecida com sucesso com conta ativa XP/Modal Mais
2. Sistema verifica horário de funcionamento da B3 (09h00–17h55 BRT) antes de enviar qualquer ordem; bloqueia ordens fora do horário
3. Funções implementadas: `get_price()`, `open_position()`, `close_position()`, `get_open_positions()`, `get_account_info()`
4. Stop loss enviado como ordem separada via `order_send()` com tipo `ORDER_TYPE_SELL_STOP` / `ORDER_TYPE_BUY_STOP`
5. Modo paper trading MT5: simula ordens sem enviar para MT5 (flag configurável)
6. Testes de integração com mocks do MT5 para fluxo completo de abertura e fechamento de posição

---

#### Story 7.2 — Adaptação do Pipeline ML para B3 e Forex

Como modelo ML,
Quero coletar dados históricos e treinar modelos específicos para WIN (mini-índice) e pares Forex,
Para que o modelo use padrões relevantes de cada mercado em vez de um modelo genérico.

**Acceptance Criteria:**
1. Dados históricos WIN e WDO baixados via MT5 (`copy_rates_from()`) para os últimos 2 anos em timeframes 5m e 1h
2. Features de mercado específicas adicionadas: sessão (pré-abertura, abertura, tarde, fechamento), dia da semana, distância para vencimento (para futuros)
3. Modelos treinados separadamente por mercado (1 modelo por símbolo): `rf_WIN_v...`, `rf_WDO_v..., `rf_EURUSD_v...`
4. Backtesting walk-forward executado para cada novo modelo com threshold de Profit Factor ≥ 1.3
5. Pipeline de retreinamento semanal estendido para incluir novos mercados
6. Dashboard exibe modelos ativos por mercado com métricas individuais

---

#### Story 7.3 — Paper Trading MT5 e Go-Live B3/Forex

Como owner do autoxtrade,
Quero validar o bot nos mercados B3 e Forex via paper trading e ativar em produção,
Para que o autoxtrade opere nos três mercados de forma confiável e autônoma.

**Acceptance Criteria:**
1. Paper trading MT5 ativo por mínimo 5 dias úteis (1 semana de pregão B3)
2. Mínimo de 5 operações simuladas por mercado (WIN e WDO) registradas
3. Horário de funcionamento B3 respeitado: zero tentativas de ordens fora do horário nos logs
4. Notificações Telegram funcionando para operações MT5 com indicação do mercado (ex: `[B3] WIN...`)
5. Checklist de go-live MT5 concluído: margem configurada, risco por trade ajustado para contratos futuros, stop loss verificado
6. Go-live com 1 contrato mínimo em WIN ou WDO como experimento inicial

---

## 7. Checklist Results Report

*A ser preenchido pelo PO (Sarah) na fase de validação de artefatos.*

---

## 8. Next Steps

### UX Expert Prompt

> Sally, temos o PRD completo do **autoxtrade** — um robô de trading pessoal com IA/ML adaptativa operando em cripto, B3 e Forex. O dashboard é web-based (Next.js), dark mode, voltado para um trader técnico que quer máxima densidade de informação com mínimo de ruído visual. Por favor, crie o Front-End Spec com as 7 telas definidas no PRD (Dashboard Principal, Posições Abertas, Histórico, Performance, Gerenciamento de Bots, Backtesting, Configurações), wireframes de fluxo e especificações de componentes. Use como referência estética o TradingView e terminais profissionais de trading. Documento base: `docs/prd.md`.

### Architect Prompt

> Winston, temos o PRD completo do **autoxtrade** — robô de trading pessoal com IA/ML, multi-mercado (Binance cripto + MT5 para B3/Forex). Stack definida: Python (FastAPI + Freqtrade + scikit-learn/TensorFlow) + Next.js + PostgreSQL + Redis + Docker. Arquitetura monolith modular em monorepo. VPS Windows necessário para MT5 na fase B3/Forex. Por favor, crie o documento de arquitetura full-stack completo, cobrindo: arquitetura de serviços, diagrama de componentes, modelo de dados completo, fluxo de dados do pipeline ML (coleta → treino → deploy → inferência → ordem), estratégia de deploy no VPS, segurança (API keys, autenticação do dashboard, firewall), e decisões de tecnologia com justificativas. Documento base: `docs/prd.md`.
