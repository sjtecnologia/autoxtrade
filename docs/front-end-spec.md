# autoxtrade — Front-End Specification

**Versão:** 1.0  
**Data:** 28 de abril de 2026  
**Status:** Rascunho  
**Agente:** UX Expert (Sally / BMad)  
**Input:** docs/prd.md v1.0

---

## Change Log

| Data | Versão | Descrição | Autor |
|---|---|---|---|
| 28/04/2026 | 1.0 | Versão inicial — spec completo do dashboard autoxtrade | UX (Sally / BMad) |

---

## 1. UX Vision & Design Philosophy

### 1.1 Filosofia Central

O dashboard do autoxtrade é uma **ferramenta de trabalho**, não um produto de consumo. O usuário (o próprio owner trader) passa horas olhando para esta interface em monitores de trading. Toda decisão de design é guiada por três princípios:

1. **Densidade primeiro** — Máxima informação relevante por centímetro de tela. Sem cards vazios, sem padding excessivo, sem animações desnecessárias.
2. **Dados em tempo real, sempre** — Nada deve precisar de refresh manual. Se um dado existe no sistema, ele aparece na tela automaticamente.
3. **Risco visível** — O estado do risco (drawdown, posições abertas, capital em risco) deve ser visível em QUALQUER tela sem precisar navegar.

### 1.2 Referências Visuais

- **TradingView** — Densidade de informação, paleta dark, tipografia monospace para preços
- **GitHub Dark** — Hierarquia visual com backgrounds em camadas (`#0d1117`, `#161b22`, `#21262d`)
- **Terminal Bloomberg** — Informação compacta, sem desperdício de espaço
- **Binance Pro** — Layout de trading com múltiplos painéis simultâneos

### 1.3 Anti-Padrões (NÃO fazer)

- ❌ Cards com muito padding e pouca informação
- ❌ Loading spinners em toda tela ao trocar de página
- ❌ Gradientes decorativos ou efeitos de glassmorphism
- ❌ Animações de entrada de elementos na tela
- ❌ Modais de confirmação excessivos (somente para ações destrutivas)
- ❌ Light mode (não existe neste sistema)

---

## 2. Design Tokens & Sistema de Design

### 2.1 Paleta de Cores

```css
/* Backgrounds — camadas de profundidade */
--bg-base:        #0d1117;  /* fundo da página */
--bg-surface:     #161b22;  /* cards, painéis */
--bg-elevated:    #21262d;  /* inputs, dropdowns, hover */
--bg-overlay:     #30363d;  /* tooltips, popovers */

/* Texto */
--text-primary:   #e6edf3;  /* texto principal */
--text-secondary: #8b949e;  /* labels, metadados */
--text-muted:     #484f58;  /* placeholders, desativado */

/* Status & Semântica */
--color-profit:   #3fb950;  /* lucro, LONG, positivo */
--color-loss:     #f85149;  /* perda, SHORT, negativo */
--color-warning:  #d29922;  /* alerta, 10% drawdown */
--color-danger:   #da3633;  /* crítico, 15% drawdown, pausa */
--color-neutral:  #58a6ff;  /* informativo, neutro, links */
--color-paper:    #bc8cff;  /* modo paper trading */

/* Bordas */
--border-subtle:  #21262d;
--border-default: #30363d;
--border-emphasis: #8b949e;

/* Drawdown Progress */
--drawdown-ok:      #3fb950;  /* 0–5% */
--drawdown-caution: #d29922;  /* 5–10% */
--drawdown-warning: #f0883e;  /* 10–15% */
--drawdown-danger:  #f85149;  /* >15% — sistema pausado */
```

### 2.2 Tipografia

```css
/* Fontes */
--font-mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
--font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;

/* Tamanhos — para valores numéricos (preços, P&L) sempre usar --font-mono */
--text-xs:   11px;  /* metadados, timestamps */
--text-sm:   12px;  /* tabelas, labels */
--text-base: 14px;  /* texto padrão */
--text-md:   16px;  /* títulos de seção */
--text-lg:   20px;  /* métricas de destaque */
--text-xl:   28px;  /* métricas principais (P&L total) */
--text-2xl:  36px;  /* equity total no header */
```

### 2.3 Espaçamento

Grid de 4px. Usar escala 4–8–12–16–24–32–48px. Nenhum padding interno de card deve ser menor que 12px nem maior que 20px para manter densidade.

### 2.4 Elevação (z-index)

```
10   — Sticky header
20   — Sidebar
100  — Dropdowns, tooltips
200  — Modais de confirmação
300  — Toast notifications
```

### 2.5 Componentes Base

| Componente | Descrição |
|---|---|
| `<MetricCard>` | Card com label, valor principal e delta (variação) |
| `<StatusBadge>` | Badge colorido: ACTIVE / PAUSED / PAPER / ERROR |
| `<PnlValue>` | Número colorido (verde/vermelho) com ícone de seta |
| `<DrawdownBar>` | Barra de progresso com cor dinâmica por threshold |
| `<MarketTag>` | Tag de mercado: CRIPTO / B3 / FOREX |
| `<TradeSide>` | Badge LONG (verde) / SHORT (vermelho) |
| `<ConfirmModal>` | Modal de confirmação para ações destrutivas |
| `<DataTable>` | Tabela paginada com ordenação e filtros |
| `<Sparkline>` | Mini gráfico de linha inline (sem eixos) |
| `<ToastAlert>` | Notificação temporária (4 segundos) no canto superior direito |

---

## 3. Layout Global

### 3.1 Estrutura Base

```
┌─────────────────────────────────────────────────────────────────┐
│  HEADER — fixo no topo, 48px                                    │
│  [logo] [status global] [equity total] [drawdown] [ws status]  │
├──────────┬──────────────────────────────────────────────────────┤
│          │                                                       │
│ SIDEBAR  │           MAIN CONTENT AREA                          │
│  64px    │           (scrollável)                               │
│ (ícones) │                                                       │
│          │                                                       │
│          │                                                       │
└──────────┴──────────────────────────────────────────────────────┘
```

### 3.2 Header (fixo, 48px de altura)

**Seção esquerda:**
- Logo `autoxtrade` + ícone de gráfico (16px, `--color-neutral`)
- Separador `|`
- Badge de modo atual: `● LIVE` (verde) / `● PAPER` (roxo) / `● PAUSADO` (vermelho)

**Seção central:**
- Equity total: `R$ 12.847,32` em `--text-2xl --font-mono --text-primary`
- Delta 24h: `+R$ 284,11 (+2.26%)` em `--font-mono --color-profit` (ou `--color-loss`)

**Seção direita:**
- Drawdown 30d: `DD: 3.4%` com cor dinâmica por threshold
- Separator
- Status WebSocket: `● WS` verde (conectado) / vermelho (desconectado) — clicável para reconectar
- Avatar/menu de configurações

### 3.3 Sidebar (64px largura — ícones apenas, sem labels)

Navegação por ícones com tooltip ao hover. Ordem de cima para baixo:

| Ícone | Tela | Tooltip |
|---|---|---|
| `LayoutDashboard` | Dashboard Principal | Dashboard |
| `Activity` | Posições Abertas | Posições |
| `History` | Histórico | Histórico |
| `BarChart2` | Performance | Performance |
| `Bot` | Gerenciamento | Bots |
| `FlaskConical` | Backtesting | Backtesting |
| `Settings` | Configurações | Configurações |

Item ativo: fundo `--bg-elevated`, borda esquerda 2px `--color-neutral`.

---

## 4. Telas — Especificações Detalhadas

---

### 4.1 Tela 1 — Dashboard Principal

**Rota:** `/`  
**Propósito:** Visão geral instantânea do sistema — métricas do dia, posições abertas, equity curve e status dos bots.

#### 4.1.1 Layout

```
┌─────────────────────────────────────────────────────┐
│  MÉTRICAS DO DIA (4 cards em linha)                 │
│  [P&L Dia]  [Win Rate]  [Operações]  [Drawdown 30d] │
├─────────────────────────────────────────────────────┤
│  POSIÇÕES ABERTAS (tabela compacta, real-time)      │
│  + STATUS DOS BOTS (painel direito, 280px)          │
├─────────────────────────────────────────────────────┤
│  EQUITY CURVE (gráfico, última semana)              │
└─────────────────────────────────────────────────────┘
```

#### 4.1.2 Faixa de Métricas do Dia

4 `<MetricCard>` em `grid-cols-4`:

| Card | Label | Valor | Delta |
|---|---|---|---|
| P&L Dia | P&L HOJE | `+R$ 284,11` (mono, verde/vermelho) | `+2.26%` |
| Win Rate | WIN RATE (30d) | `62.5%` | `+3.1% vs mês ant.` |
| Operações | OPERAÇÕES HOJE | `7` | `4W / 3L` |
| Drawdown | MAX DD (30d) | `3.4%` | Barra de progresso colorida |

Cada card: `bg-surface`, border `--border-subtle`, padding 16px, label em `--text-secondary --text-xs uppercase tracking-wider`, valor em `--text-xl --font-mono`.

#### 4.1.3 Tabela de Posições Abertas

Tabela compacta com atualização via WebSocket. Linhas com 36px de altura.

**Colunas:**
| # | Campo | Tipo | Exemplo |
|---|---|---|---|
| 1 | Símbolo | `<MarketTag>` + texto | `CRIPTO BTC/USDT` |
| 2 | Lado | `<TradeSide>` | `LONG` (badge verde) |
| 3 | Entrada | mono right-align | `R$ 287.450,00` |
| 4 | Atual | mono right-align, atualiza em tempo real | `R$ 290.120,00` |
| 5 | P&L | `<PnlValue>` | `+R$ 267,00 (+0.93%)` |
| 6 | Tempo aberto | relativo | `2h 14m` |
| 7 | Stop Loss | mono, vermelho suave | `R$ 282.900,00` |
| 8 | Take Profit | mono, verde suave | `R$ 294.000,00` |
| 9 | Ação | botão `×` (fechar manualmente) | — |

Linha hover: fundo `--bg-elevated`. Linha P&L positivo: borda esquerda 2px `--color-profit`. Linha P&L negativo: borda esquerda 2px `--color-loss`.

Caso não haja posições abertas: estado vazio com mensagem `Nenhuma posição aberta` em `--text-muted`, ícone de repouso.

#### 4.1.4 Painel de Status dos Bots (direita, 280px)

Para cada mercado configurado:

```
┌──────────────────────────┐
│ ● CRIPTO    [ATIVO]      │
│ Binance · BTC/USDT       │
│ Modelo: rf_v20260428     │
│ PF: 1.82 · Uptime: 99.8%│
│ [Pausar]                 │
├──────────────────────────┤
│ ○ B3        [INATIVO]    │
│ MT5 · WIN, WDO           │
│ Fase 1b — não iniciada   │
└──────────────────────────┘
```

Bot ATIVO: dot verde, badge `<StatusBadge status="active">`.  
Bot PAUSADO: dot vermelho, badge `<StatusBadge status="paused">`.  
Bot PAPER: dot roxo, badge `<StatusBadge status="paper">`.

Botão "Pausar"/"Retomar" abre `<ConfirmModal>` antes de executar.

#### 4.1.5 Equity Curve

Gráfico de linha (Recharts `LineChart`) com:
- Período padrão: últimos 7 dias (selector: 7D / 30D / 3M / Tudo)
- Linha única: equity total em R$
- Área preenchida abaixo da linha com gradiente suave `--color-profit` com opacidade 15%
- Eixo X: timestamps (dia/hora); Eixo Y: valor em R$
- Linha de referência horizontal no valor de capital inicial (tracejada, `--text-muted`)
- Tooltip: data, equity naquele ponto, delta desde início
- Altura: 180px

---

### 4.2 Tela 2 — Posições Abertas

**Rota:** `/positions`  
**Propósito:** Visão expandida de todas as posições com mais detalhes por operação.

#### 4.2.1 Layout

```
┌──────────────────────────────────────────────────────┐
│  RESUMO RÁPIDO (3 métricas em linha)                 │
│  [Total em risco R$]  [P&L não realizado]  [# posições] │
├──────────────────────────────────────────────────────┤
│  FILTRO: [Todos] [CRIPTO] [B3] [FOREX]               │
├──────────────────────────────────────────────────────┤
│  TABELA EXPANDIDA (mais colunas)                     │
└──────────────────────────────────────────────────────┘
```

#### 4.2.2 Tabela Expandida de Posições

Mesmas colunas da tela principal, adicionando:

| Campo extra | Descrição |
|---|---|
| Exchange | Binance / MT5-XP |
| Tamanho | Quantidade em unidades (0.00234 BTC) |
| Valor nominal | Valor total da posição em R$ |
| R:R atual | Risk:Reward atual calculado em tempo real |
| Modelo | Versão do modelo ML que gerou o sinal |
| Confiança | Confidence score da predição (ex: 0.73) |

Tabela responsiva com scroll horizontal em telas < 1400px.

#### 4.2.3 Ação de Fechar Posição

Clique no botão `×` abre modal:

```
┌─────────────────────────────────┐
│  Fechar posição manualmente?    │
│                                 │
│  BTC/USDT LONG                  │
│  Entrada: R$ 287.450,00         │
│  Atual:   R$ 290.120,00         │
│  P&L est: +R$ 267,00 (+0.93%)   │
│                                 │
│  [Cancelar]  [Confirmar fechar] │
└─────────────────────────────────┘
```

"Confirmar fechar" com estilo destrutivo (`--color-danger` background).

---

### 4.3 Tela 3 — Histórico de Operações

**Rota:** `/history`  
**Propósito:** Consulta e análise de todas as operações fechadas com filtros completos.

#### 4.3.1 Layout

```
┌────────────────────────────────────────────────────────┐
│  FILTROS (barra horizontal)                            │
│  [Período ▼] [Mercado ▼] [Resultado ▼] [Ativo ▼] [CSV]│
├────────────────────────────────────────────────────────┤
│  RESUMO DO FILTRO ATIVO (4 métricas)                   │
│  [Total P&L] [Win Rate] [# Trades] [Profit Factor]     │
├────────────────────────────────────────────────────────┤
│  TABELA DE HISTÓRICO (paginada, 50 por página)         │
└────────────────────────────────────────────────────────┘
```

#### 4.3.2 Filtros

| Filtro | Opções |
|---|---|
| Período | Hoje / Esta semana / Este mês / Último mês / 3 meses / Personalizado (date picker) |
| Mercado | Todos / Cripto / B3 / Forex |
| Resultado | Todos / Lucro / Perda |
| Ativo | Input de busca livre (ex: "BTC", "WIN") |

Todos os filtros atualizam a tabela e as métricas de resumo imediatamente sem recarregar a página.

#### 4.3.3 Tabela de Histórico

| Coluna | Exemplo |
|---|---|
| Data/Hora abertura | `28/04 14:32` |
| Símbolo | `BTC/USDT` |
| Mercado | `<MarketTag>CRIPTO</MarketTag>` |
| Lado | `<TradeSide>LONG</TradeSide>` |
| Entrada | `R$ 287.450` |
| Saída | `R$ 290.120` |
| Tamanho | `0.00234 BTC` |
| P&L R$ | `<PnlValue>+R$ 267,00</PnlValue>` |
| P&L % | `<PnlValue>+0.93%</PnlValue>` |
| Duração | `2h 14m` |
| Fechamento | Badge: `TP HIT` / `SL HIT` / `MANUAL` / `DD LIMIT` |
| Modelo | `rf_v20260428` (tooltip com métricas do modelo) |

Linhas clicáveis: expandem um drawer lateral com todos os detalhes da operação (inclusive log de eventos).

#### 4.3.4 Drawer de Detalhes do Trade

Painel deslizante da direita (400px) ao clicar em uma linha:

```
DETALHES DA OPERAÇÃO #1042
──────────────────────────
Símbolo:    BTC/USDT (CRIPTO)
Lado:       LONG
Status:     Fechado — TP HIT

Abertura:   28/04/2026 14:32:18
Fechamento: 28/04/2026 16:46:03
Duração:    2h 13m 45s

Entrada:    R$ 287.450,00
Saída:      R$ 290.120,00
Quantidade: 0.00234 BTC
Valor nom.: R$ 672,63

Stop Loss:  R$ 282.900,00
Take Profit: R$ 290.120,00

P&L Bruto:  +R$ 267,00
Corretagem: -R$ 1,34
P&L Líquido: +R$ 265,66 (+0.93%)

Modelo ML:  rf_v20260428_143022
Sinal:      LONG (confidence: 0.78)
Regime:     Tendência de alta

TIMELINE DE EVENTOS
──────────────────
14:32:18  Sinal gerado (LONG, conf: 0.78)
14:32:19  Ordem enviada (limit, 287.450)
14:32:21  Ordem executada
14:32:22  OCO configurado (SL: 282.900, TP: 290.120)
16:46:03  Take profit atingido
16:46:03  Posição fechada
16:46:03  Notificação Telegram enviada
```

---

### 4.4 Tela 4 — Performance & Métricas

**Rota:** `/performance`  
**Propósito:** Análise aprofundada de performance por período e por mercado, com gráficos.

#### 4.4.1 Layout

```
┌──────────────────────────────────────────────────────────┐
│  SELECTOR DE PERÍODO: [7D] [30D] [3M] [6M] [1A] [Tudo] │
├────────────────┬─────────────────────────────────────────┤
│                │  MÉTRICAS PRINCIPAIS (grid 3x2)         │
│  EQUITY CURVE  │  [Win Rate] [Profit Factor] [Sharpe]    │
│  (gráfico      │  [Max DD]   [Avg R:R]       [# Trades]  │
│   principal,   │                                         │
│   280px alto)  │  MÉTRICAS POR MERCADO (tabela)          │
│                │  Cripto / B3 / Forex                    │
├────────────────┴─────────────────────────────────────────┤
│  DISTRIBUIÇÃO DE RESULTADOS (histograma de P&L %)        │
├──────────────────────────────────────────────────────────┤
│  PERFORMANCE MENSAL (heatmap tipo GitHub contributions)  │
└──────────────────────────────────────────────────────────┘
```

#### 4.4.2 Métricas Principais

6 `<MetricCard>` em `grid-cols-3`:

| Métrica | Cálculo | Benchmark |
|---|---|---|
| Win Rate | trades lucrativos / total | Meta: ≥ 55% |
| Profit Factor | lucro bruto / perda bruta | Meta: ≥ 1.5 |
| Sharpe Ratio | retorno ajustado ao risco | Meta: ≥ 1.0 |
| Max Drawdown | maior queda pico a vale | Limite: 15% |
| Avg R:R | média de risco:retorno realizado | Meta: ≥ 1.5 |
| Total Trades | contagem no período | — |

Cada card inclui indicador de tendência (seta cima/baixo vs período anterior).

#### 4.4.3 Métricas por Mercado

Tabela simples com uma linha por mercado:

| Mercado | Trades | Win Rate | PF | P&L Total |
|---|---|---|---|---|
| Cripto | 142 | 63.4% | 1.87 | +R$ 2.841 |
| B3 | — | — | — | — |
| Forex | — | — | — | — |

#### 4.4.4 Histograma de Distribuição de P&L

Gráfico de barras (Recharts `BarChart`) mostrando frequência de trades por faixa de P&L (%):
- Faixas: < -3%, -3 a -2%, -2 a -1%, -1 a 0%, 0 a +1%, +1 a +2%, +2 a +3%, > +3%
- Barras negativas em `--color-loss`; positivas em `--color-profit`
- Linha vertical tracejada no zero

#### 4.4.5 Heatmap Mensal de Performance

Grid de quadradinhos (similar ao GitHub contribution graph) — 1 quadrado por dia:
- Verde escuro → verde brilhante: dias com lucro (escala pela magnitude)
- Vermelho: dias com perda
- Cinza: dias sem operação
- Tooltip ao hover: data, P&L do dia, número de trades

---

### 4.5 Tela 5 — Gerenciamento de Bots

**Rota:** `/bots`  
**Propósito:** Controle operacional completo — pausar/retomar bots, visualizar modelos ML ativos, alterar parâmetros de risco.

#### 4.5.1 Layout

```
┌────────────────────────────────────────────────────┐
│  CARD CRIPTO (estado completo)                     │
│  [Status] [Parâmetros de risco] [Modelo ML ativo] │
│  [Pausar / Retomar]                                │
├────────────────────────────────────────────────────┤
│  CARD B3 (estado completo)                         │
├────────────────────────────────────────────────────┤
│  CARD FOREX (estado completo)                      │
├────────────────────────────────────────────────────┤
│  HISTÓRICO DE MODELOS ML                           │
└────────────────────────────────────────────────────┘
```

#### 4.5.2 Card de Bot por Mercado

Cada mercado tem um card expandido com seções:

**Seção de Status:**
```
● CRIPTO — Binance                           [ATIVO]
Uptime: 99.8% · Última execução: há 3min
Operações abertas: 2 / 3 máx
```

**Seção de Risco (editável inline):**
```
Risco por trade:    [1.0%  ▲▼]   (0.1% – 5.0%)
Max drawdown:       [15.0% ▲▼]   (5% – 30%)
Max posições simult:[3     ▲▼]   (1 – 10)
Pares habilitados:  [BTC/USDT ×] [ETH/USDT ×] [+ Adicionar]
```

Inputs de risco com validação inline. Botão `Salvar alterações` aparece ao detectar mudança (sticky no fundo do card).

**Seção do Modelo ML:**
```
Modelo ativo:   rf_v20260428_143022
Treinado em:    28/04/2026 14:30
Profit Factor:  1.82 (backtesting)
Accuracy:       68.3%
Próximo treino: 05/05/2026 02:00

[Forçar retreino] [Rollback para rf_v20260421]
```

**Ações do Bot:**
- Botão `Pausar` (amarelo) — abre ConfirmModal
- Botão `Ativar` (verde) — abre ConfirmModal
- Switch `Modo Paper Trading` — toggle com ConfirmModal

#### 4.5.3 Histórico de Modelos ML

Tabela com todas as versões de modelos:

| Versão | Data treino | Status | PF Backtest | Acc. | Ação |
|---|---|---|---|---|---|
| rf_v20260428 | 28/04 14:30 | `ACTIVE` (verde) | 1.82 | 68.3% | — |
| rf_v20260421 | 21/04 02:00 | `inactive` | 1.74 | 66.1% | [Restaurar] |
| rf_v20260414 | 14/04 02:00 | `inactive` | 1.63 | 64.8% | [Restaurar] |
| rf_v20260407 | 07/04 02:00 | `rejected` (vermelho) | 1.21 | 61.2% | — |

Linha `rejected` em `--text-muted` com strikethrough no PF.

---

### 4.6 Tela 6 — Backtesting

**Rota:** `/backtest`  
**Propósito:** Configurar e executar backtests de estratégias antes de ativação, visualizar resultados detalhados.

#### 4.6.1 Layout

```
┌─────────────────────────────────────────────────────┐
│  CONFIGURAÇÃO DO BACKTEST (formulário lateral 320px)│
│  + RESULTADOS DO ÚLTIMO BACKTEST (área principal)   │
├─────────────────────────────────────────────────────┤
│  EQUITY CURVE DO BACKTEST                           │
├─────────────────────────────────────────────────────┤
│  TRADES DO BACKTEST (tabela de simulação)           │
└─────────────────────────────────────────────────────┘
```

#### 4.6.2 Formulário de Configuração

Painel lateral esquerdo (320px, fixo):

```
CONFIGURAR BACKTEST
──────────────────
Mercado:    [Cripto       ▼]
Par:        [BTC/USDT     ▼]
Timeframe:  [1h           ▼]
Período:    [01/01/2024] a [28/04/2026]

Estratégia
──────────
Modelo:     [Modelo atual ▼]
Tipo:       [Walk-forward ▼]
  └─ Janela treino:  [12 meses]
  └─ Janela teste:   [ 3 meses]

Capital inicial: [R$ 10.000]
Risk per trade:  [1.0%]
Max posições:    [3]

[▶ Executar Backtest]
```

Estado durante execução: botão vira `⏳ Executando...` (desabilitado), barra de progresso aparece abaixo.

#### 4.6.3 Resultados do Backtest

**Métricas Resumo (grid 3x2):**

| Métrica | Valor | Benchmark |
|---|---|---|
| Retorno Total | `+184.3%` | buy-and-hold comparação |
| Profit Factor | `1.82` | ≥ 1.3 para aprovar |
| Sharpe Ratio | `1.64` | ≥ 1.0 |
| Max Drawdown | `8.7%` | ≤ 15% |
| Win Rate | `63.4%` | ≥ 55% |
| Total Trades | `312` | mínimo 100 |

Indicadores visuais: checkmark verde se atingir benchmark, X vermelho se não atingir. Faixa de aprovação/rejeição explícita:

```
✅ Modelo APROVADO para produção (todos os critérios atingidos)
   [Ativar em Produção] [Salvar Resultado] [Exportar CSV]
```

ou

```
❌ Modelo REJEITADO (Profit Factor 1.21 < 1.30 mínimo)
   [Ajustar Parâmetros] [Exportar Relatório]
```

**Equity Curve do Backtest:**

Gráfico Recharts `LineChart` com 3 séries sobrepostas:
1. Estratégia backtest (linha branca principal)
2. Buy-and-hold (linha tracejada cinza — comparação)
3. Drawdown (área preenchida vermelha no eixo secundário Y, abaixo do zero)

**Tabela de Trades Simulados:**
Mesma estrutura da tela de histórico, indicando que são trades simulados com badge `SIMULADO`.

---

### 4.7 Tela 7 — Configurações

**Rota:** `/settings`  
**Propósito:** Parâmetros globais do sistema, configuração de notificações e informações do sistema.

#### 4.7.1 Layout

Página com seções em acordeão (expandidas por padrão):

```
▼ NOTIFICAÇÕES TELEGRAM
▼ PARÂMETROS GLOBAIS DE RISCO
▼ CONEXÕES & APIs
▼ SISTEMA & MANUTENÇÃO
```

#### 4.7.2 Seção: Notificações Telegram

```
Token do Bot:    [●●●●●●●●●●●●●●●●]  [Testar conexão]
Chat ID:         [●●●●●●●●]           [Enviar mensagem teste]

Notificações ativas:
[✓] Abertura de posição
[✓] Fechamento de posição
[✓] Alerta de drawdown (10%)
[✓] Pausa automática (15%)
[✓] Relatório diário (23h)
[✓] Erros críticos
[ ] Retreinamento de modelo concluído
```

#### 4.7.3 Seção: Parâmetros Globais de Risco

```
Drawdown máximo global:   [15%]   (pausa todos os bots)
Drawdown alerta:          [10%]   (apenas notificação)
Max capital total em risco de uma vez: [20%]
```

#### 4.7.4 Seção: Conexões & APIs

Para cada conexão configurada:

```
BINANCE API
Status: ● Conectado (verificado há 2min)
Permissões: [TRADE ✓] [WITHDRAW ✗] [READ ✓]
Pares configurados: BTC/USDT, ETH/USDT
[Testar Conexão] [Revogar e Reconfigurar]

MT5 (B3/FOREX) — Fase 1b
Status: ○ Não configurado
[Configurar MT5]
```

⚠️ As API keys em si NUNCA são exibidas no dashboard — apenas status de conexão e permissões.

#### 4.7.5 Seção: Sistema & Manutenção

```
Versão do sistema:  v0.1.0 (build 20260428)
Banco de dados:     PostgreSQL 16 · 142MB
Redis:              Conectado · 12MB em uso
Uptime do backend:  3d 4h 22m

[Forçar backup manual]
[Limpar cache Redis]
[Baixar logs dos últimos 30 dias]
[Reiniciar serviços]  ← com ConfirmModal
```

---

## 5. Fluxos de Interação Críticos

### 5.1 Fluxo: Pausar Bot

```
1. Owner clica [Pausar] no card do bot (Tela 5 ou Header)
2. ConfirmModal abre:
   "Pausar bot CRIPTO?
   Posições abertas (2) continuarão abertas com stops ativos.
   Nenhuma nova posição será aberta.
   [Cancelar] [Confirmar pausar]"
3. Confirmação → POST /api/v1/config {market: "cripto", is_active: false}
4. Toast: "Bot CRIPTO pausado"
5. Notificação Telegram enviada
6. Badge no header e no card atualizam para PAUSADO (vermelho)
7. Sidebar badge atualiza
```

### 5.2 Fluxo: Alerta de Drawdown Automático

```
[Automático — sem ação do owner]

1. Sistema detecta drawdown 30d ≥ 10%
2. Header: "DD: 10.2%" muda para amarelo pulsante
3. Toast: "⚠️ Alerta: Drawdown atingiu 10.2%"
4. Telegram: mensagem de alerta

Se drawdown continua e atinge 15%:
5. Header: faixa vermelha "TRADING PAUSADO — Drawdown 15.4%"
6. Todos os bots mudam status para PAUSADO
7. Botão "Retomar trading" aparece no header (requer confirmação)
8. Toast vermelho persistente (não desaparece automaticamente)
9. Telegram: mensagem de emergência
```

### 5.3 Fluxo: Novo Modelo ML Disponível

```
[Automático — retreinamento semanal concluído]

1. Toast: "✅ Novo modelo disponível: rf_v20260505 (PF: 1.91)"
2. Badge no painel de bot (Tela 5): "Modelo novo disponível"
3. Owner navega para Tela 5
4. Seção de modelo mostra comparação:
   Atual:   rf_v20260428 — PF: 1.82
   Novo:    rf_v20260505 — PF: 1.91 (recomendado ✅)
5. Botão [Ativar novo modelo] com ConfirmModal
6. Confirmação → ativa novo modelo, anterior vai para inactive
7. Toast: "Modelo rf_v20260505 ativado"
```

### 5.4 Fluxo: WebSocket Desconectado

```
1. Conexão WS cai
2. Header: badge WS muda para vermelho
3. Tentativa automática de reconexão (exponential backoff: 1s, 2s, 4s, 8s, max 30s)
4. Dados da tela mostram indicador "⚠️ Dados podem estar desatualizados"
5. Ao reconectar: badge volta para verde, toast "Conexão restabelecida", dados atualizam
```

---

## 6. Estados da Interface

### 6.1 Estados de Loading

- **Skeleton screens** em vez de spinners para conteúdo de tabelas e gráficos
- Skeleton: retângulos cinza animados (`--bg-elevated` com `animation: pulse`) no lugar dos dados
- Spinners apenas em botões de ação (ex: "Executar Backtest")

### 6.2 Estados Vazios (Empty States)

| Contexto | Mensagem | Ação sugerida |
|---|---|---|
| Sem posições abertas | "Nenhuma posição aberta no momento" | — |
| Sem histórico no período | "Nenhuma operação no período selecionado" | Ampliar filtro de datas |
| Sem backtests executados | "Execute um backtest para ver os resultados" | [Configurar backtest] |
| B3/Forex não configurado | "Mercado não configurado — Fase 1b" | [Ver roadmap] |

### 6.3 Estados de Erro

| Tipo | Exibição |
|---|---|
| API offline | Banner no topo da página: "Backend offline — dados podem estar desatualizados" |
| Exchange offline | Badge no painel do bot: "Exchange inacessível" + timestamp do último status |
| Erro ao salvar config | Toast vermelho: mensagem de erro específica; dados não são alterados |
| Backtest falhado | Área de resultados exibe mensagem de erro com detalhes técnicos |

---

## 7. Responsividade

### 7.1 Breakpoints

| Breakpoint | Largura | Ajuste |
|---|---|---|
| Desktop L | ≥ 1440px | Layout padrão completo |
| Desktop M | 1280–1439px | Colunas de tabelas reduzidas; painel lateral colapsado |
| Desktop S | 1024–1279px | Sidebar em modo tooltip-only; grids passam de 4 para 2 colunas |
| Tablet | 768–1023px | Layout de coluna única; tabelas com scroll horizontal |
| Mobile | < 768px | Não suportado — exibe mensagem de redirecionamento |

### 7.2 Comportamento Mobile

Resolução < 768px: tela inteira exibe:
```
O dashboard do autoxtrade é otimizado para desktop.
Acesse em um monitor para melhor experiência.

Para operações urgentes, utilize o Telegram Bot.
```

---

## 8. Acessibilidade

### 8.1 Requisitos MVP

- Contraste mínimo 4.5:1 para texto principal (`--text-primary` em `--bg-surface` ✓)
- Contraste mínimo 3:1 para texto secundário
- Todos os botões de ação com `aria-label` descritivo
- Modais de confirmação com foco gerenciado (focus trap) e fechamento via Escape
- Tabelas com `role="grid"` e headers com `scope`

### 8.2 Fora de Escopo (MVP)

Screen reader otimizado, modo de alto contraste, internacionalização.

---

## 9. Stack de Frontend

### 9.1 Tecnologias

| Tecnologia | Versão | Justificativa |
|---|---|---|
| Next.js | 14+ (App Router) | SSR + SPA híbrido; roteamento nativo |
| TypeScript | 5+ | Tipagem estrita; essencial para dados financeiros |
| TailwindCSS | 3+ | Utility-first; custom design tokens via CSS vars |
| Recharts | 2+ | Gráficos React nativos (equity curve, histograma) |
| Zustand | 4+ | Estado global leve (posições, config, ws status) |
| React Query | 5+ | Cache + sync de dados da API REST |
| Socket.io-client | 4+ | WebSocket gerenciado com reconexão automática |
| Lucide React | — | Ícones consistentes (mesma lib do shadcn) |
| date-fns | 3+ | Manipulação de datas em pt-BR |

### 9.2 Estrutura de Pastas (Frontend)

```
frontend/
├── app/                      # Next.js App Router
│   ├── layout.tsx            # Layout global (header + sidebar)
│   ├── page.tsx              # Dashboard principal (/)
│   ├── positions/page.tsx    # Posições abertas
│   ├── history/page.tsx      # Histórico
│   ├── performance/page.tsx  # Performance
│   ├── bots/page.tsx         # Gerenciamento
│   ├── backtest/page.tsx     # Backtesting
│   └── settings/page.tsx     # Configurações
├── components/
│   ├── ui/                   # Componentes base reutilizáveis
│   │   ├── MetricCard.tsx
│   │   ├── StatusBadge.tsx
│   │   ├── PnlValue.tsx
│   │   ├── DrawdownBar.tsx
│   │   ├── DataTable.tsx
│   │   ├── ConfirmModal.tsx
│   │   └── ToastAlert.tsx
│   ├── charts/               # Componentes de gráfico
│   │   ├── EquityCurve.tsx
│   │   ├── PnlHistogram.tsx
│   │   └── PerformanceHeatmap.tsx
│   ├── dashboard/            # Componentes específicos por tela
│   ├── positions/
│   ├── history/
│   ├── bots/
│   └── backtest/
├── hooks/
│   ├── useWebSocket.ts       # Hook de conexão WS com reconexão
│   ├── usePositions.ts       # Estado de posições em tempo real
│   └── useDrawdown.ts        # Monitor de drawdown global
├── store/
│   └── botStore.ts           # Estado global (Zustand)
├── lib/
│   ├── api.ts                # Cliente API (axios/fetch wrapper)
│   ├── formatters.ts         # Formatação de moeda, datas, %
│   └── constants.ts          # Thresholds de drawdown, cores
└── styles/
    └── globals.css           # Design tokens CSS vars
```

### 9.3 Convenções de Código

- **Números financeiros:** sempre usar `Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' })` — nunca formatar manualmente
- **Timestamps:** sempre armazenar em UTC; exibir em UTC-3 (BRT) usando `date-fns-tz`
- **WebSocket:** implementar reconexão com backoff exponencial; nunca deixar conexão morta sem feedback visual
- **Testes:** Jest + React Testing Library para componentes críticos (MetricCard, PnlValue, DrawdownBar)

---

## 10. Próximos Passos

### Architect Prompt

> Winston, temos o Front-End Spec completo do **autoxtrade**. Stack frontend: Next.js 14 (App Router) + TypeScript + TailwindCSS + Recharts + Zustand + React Query + Socket.io-client. 7 telas definidas com componentes e fluxos detalhados. O frontend consome API REST + WebSocket do backend FastAPI. Por favor, crie o documento de arquitetura full-stack completo incluindo: diagrama de componentes (backend + frontend), modelo de dados PostgreSQL completo (todos os campos das tabelas), fluxo de dados do pipeline ML (coleta → treino → deploy → inferência → ordem → feedback), estratégia de deploy no VPS Windows/Linux com Docker Compose, configuração de Nginx, segurança (autenticação do dashboard, rotação de credenciais), e contrato de API (endpoints REST + eventos WebSocket) para integração com este front-end spec. Documentos base: `docs/prd.md` + `docs/front-end-spec.md`.
