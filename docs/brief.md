# Project Brief: autoxtrade

**Versão:** 1.0  
**Data:** 28 de abril de 2026  
**Status:** Rascunho  

---

## Executive Summary

O **autoxtrade** é uma plataforma pessoal de trading automatizado baseada em Inteligência Artificial e Machine Learning, capaz de operar simultaneamente em múltiplos mercados (criptomoedas, Bolsa Brasileira B3 e Forex). O produto resolve o problema de execução emocional, lentidão humana e falta de operação contínua (24/7) nas negociações financeiras, entregando decisões de entrada e saída baseadas em dados, com otimização contínua via IA — algo que os concorrentes existentes fazem de forma superficial ou engessada.

---

## Problem Statement

### Estado Atual e Dores

Traders individuais enfrentam três grandes obstáculos ao operar manualmente:

1. **Emoção nas decisões:** Medo e ganância levam a entradas tardias, saídas prematuras e overtrading, destruindo resultados mesmo com estratégias válidas.
2. **Impossibilidade de operar 24/7:** Cripto opera ininterruptamente; B3 tem janelas de horário exigindo monitoramento constante; Forex cobre múltiplos fusos. Nenhum humano consegue acompanhar todos simultaneamente.
3. **Estratégias estáticas:** As plataformas existentes (3Commas, Cryptohopper, MetaTrader) executam regras pré-programadas fixas que não se adaptam a mudanças de regime de mercado.

### Por Que Soluções Existentes Falham

- **3Commas / Cryptohopper:** Focados em cripto, estratégias rígidas baseadas em indicadores clássicos, sem aprendizado adaptativo real.
- **MetaTrader 5:** Requer programação em MQL5, curva de aprendizado alta, sem IA nativa adaptativa.
- **Profit Pro / Tryd (B3):** Limitados ao mercado brasileiro, sem multi-mercado, sem ML.
- **Nenhum** oferece um modelo unificado que opere cripto + B3 + Forex com IA que aprende e se adapta continuamente ao comportamento do mercado.

### Urgência

Mercados financeiros evoluem constantemente. Uma estratégia que funciona hoje pode ser ineficaz em 3 meses. Adaptação contínua via ML não é um diferencial futuro — é uma necessidade presente.

---

## Proposed Solution

O **autoxtrade** é um robô de trading pessoal com IA adaptativa que:

- **Opera automaticamente** em cripto (Binance, Bybit, OKX), B3 (via API de corretoras como XP, Clear, Rico) e Forex (MetaTrader/Broker API), a partir de uma única interface.
- **Usa ML para aprender** padrões de mercado e ajustar dinamicamente parâmetros de entrada/saída, stop loss, take profit e tamanho de posição.
- **Minimiza risco** via gestão de capital inteligente (Kelly Criterion, position sizing dinâmico, correlação entre ativos).
- **Backtesta automaticamente** novas estratégias antes de aplicá-las em conta real.
- **Aprende com os próprios resultados:** Cada operação realizada retroalimenta o modelo de ML, melhorando progressivamente a precisão.

### Diferencial Central

> Enquanto concorrentes executam regras fixas, o autoxtrade **aprende o comportamento do mercado** e **adapta sua estratégia em tempo real**, com foco simultâneo em múltiplos mercados e gerenciamento de risco inteligente.

---

## Target Users

### Segmento Primário: Trader Individual Técnico

**Perfil:**
- Idade: 25–45 anos
- Experiência: Intermediário a avançado em trading (conhece análise técnica, sabe o que são indicadores como RSI, MACD, Bollinger Bands)
- Possui conta em pelo menos uma exchange de cripto e/ou corretora de valores
- Familiarizado com tecnologia, mas não necessariamente programador

**Comportamento atual:**
- Monitora charts manualmente no TradingView
- Faz operações manuais perdendo oportunidades por não estar disponível 24/7
- Testa robôs prontos mas se frustra com rigidez das estratégias

**Necessidades:**
- Execução automática e confiável das operações
- Visibilidade total sobre o que o robô está fazendo e por quê
- Controle de risco real (não perder tudo em uma operação ruim)
- Desempenho superior ao buy-and-hold ao longo do tempo

**Objetivo:** Fazer o dinheiro trabalhar de forma inteligente sem precisar monitorar telas o dia inteiro.

> **Nota MVP:** Para o MVP, o produto é de **uso pessoal exclusivo** — um único usuário (o próprio desenvolvedor/owner). Não há gestão multi-usuário nesta fase.

---

## Goals & Success Metrics

### Business Objectives

- Ter um robô 100% funcional operando nos 3 mercados (cripto, B3, Forex) em até **6 meses** de desenvolvimento
- Atingir **ROI positivo** comparado ao buy-and-hold após 3 meses de operação real
- **Drawdown máximo** controlado abaixo de 15% do capital em qualquer janela de 30 dias
- **Taxa de acerto** das operações acima de 55% combinada com relação risco/retorno ≥ 1.5:1

### User Success Metrics

- Sistema executa operações 24/7 sem necessidade de intervenção manual
- Notificações claras sobre cada operação realizada (entrada, saída, resultado)
- Dashboard mostra P&L em tempo real por mercado e consolidado
- Tempo médio de resposta do modelo ML a mudanças de mercado < 5 minutos

### Key Performance Indicators (KPIs)

- **Win Rate:** % de operações lucrativas — meta: ≥ 55%
- **Profit Factor:** (lucro bruto / perda bruta) — meta: ≥ 1.5
- **Sharpe Ratio:** Retorno ajustado ao risco — meta: ≥ 1.0 anualizado
- **Max Drawdown:** Queda máxima do capital — limite: ≤ 15%
- **Uptime do sistema:** Disponibilidade do robô — meta: ≥ 99.5%
- **Latência de execução:** Tempo entre sinal e ordem enviada — meta: ≤ 500ms

---

## MVP Scope

### Core Features (Must Have)

- **Conexão com exchanges/corretoras via API:** Integração com Binance (cripto) e pelo menos uma corretora B3 (ex: Clear/XP via API) para início. Forex como terceira integração.
- **Engine de execução de ordens:** Envio automático de ordens de compra e venda com stop loss e take profit configurados.
- **Modelo de ML básico:** Modelo inicial de classificação (ex: Random Forest ou LSTM) treinado com dados históricos OHLCV + indicadores técnicos para prever direção do preço.
- **Gestão de risco:** Position sizing dinâmico (risco por operação configurável, ex: máx 2% do capital), stop loss automático obrigatório em toda operação.
- **Backtesting:** Módulo para testar estratégias em dados históricos antes de ativar em conta real.
- **Dashboard básico:** Interface web para visualizar operações abertas, histórico, P&L, e status do robô por mercado.
- **Sistema de notificações:** Alertas via Telegram a cada operação (abertura, fechamento, resultado).
- **Paper trading mode:** Modo de simulação com dados reais sem executar ordens de fato (para validação antes de go-live).

### Out of Scope para MVP

- Multi-usuário / SaaS / monetização
- Mobile app nativo (iOS/Android)
- Copy trading (copiar estratégias de terceiros)
- Trading de opções e derivativos complexos
- Integração com mais de 3 corretoras/exchanges simultaneamente
- Interface de criação visual de estratégias (drag-and-drop)
- Relatórios fiscais / IR automático
- Suporte a múltiplas contas simultâneas

### MVP Success Criteria

O MVP será considerado bem-sucedido quando:
1. O robô executar operações reais autonomamente por 30 dias consecutivos sem intervenção manual
2. O resultado consolidado for positivo ou neutro (sem destruição de capital)
3. O drawdown máximo se mantiver abaixo de 15%
4. O dashboard mostrar dados precisos e atualizados em tempo real

---

## Post-MVP Vision

### Phase 2 Features

- Refinamento do modelo ML com dados próprios acumulados (aprendizado contínuo)
- Expansão para mais exchanges (Bybit, OKX, Kraken) e corretoras B3
- Integração com TradingView Webhooks para sinais externos
- Interface de configuração de parâmetros de estratégia sem código
- Relatório de performance mensal automatizado (PDF)
- Otimização de portfólio com teoria moderna de carteiras (Markowitz / Black-Litterman)

### Long-term Vision (12–24 meses)

Evoluir o autoxtrade para um sistema de trading adaptativo de ponta, com modelo de IA capaz de:
- Detectar automaticamente regime de mercado (tendência, lateral, alta volatilidade)
- Ajustar estratégia conforme o regime sem intervenção humana
- Incorporar análise de sentimento de notícias e redes sociais como feature do modelo
- Potencial abertura como SaaS para outros traders após validação pessoal completa

### Expansion Opportunities

- Transformar em produto SaaS com planos de assinatura após validação do MVP pessoal
- Marketplace de estratégias (usuários compartilham modelos)
- Integração com corretoras internacionais (Interactive Brokers, Alpaca para mercado americano)

---

## Technical Considerations

### Platform Requirements

- **Target Platforms:** Web (dashboard responsivo) + backend server 24/7
- **Browser Support:** Chrome, Firefox, Safari — últimas 2 versões major
- **Performance Requirements:** Latência de execução de ordens ≤ 500ms; processamento de dados de mercado em tempo real via WebSocket

### Technology Preferences

- **Frontend:** Next.js (React) + TailwindCSS — dashboard de monitoramento
- **Backend:** Python (FastAPI) — linguagem dominante em ML/trading
- **ML/AI:** scikit-learn (Random Forest), TensorFlow/Keras (LSTM para séries temporais), pandas/numpy para análise de dados
- **Cripto (MVP fase 1):** [Freqtrade](https://www.freqtrade.io) (framework open-source com ML integrado) + [CCXT](https://github.com/ccxt/ccxt) — Binance como exchange inicial
- **B3 + Forex (fase 1b/2):** [MetaTrader 5 Python API](https://pypi.org/project/MetaTrader5/) — biblioteca oficial que envia ordens para MT5; conta gratuita via XP ou Modal Mais já inclui MT5 sem custo adicional
- **Banco de dados:** PostgreSQL (dados históricos, operações, configurações) + Redis (cache de dados em tempo real)
- **Hospedagem/Infraestrutura:** VPS Windows (necessário para MT5) ou VPS Linux para fase cripto-only; DigitalOcean/AWS ~$20–50/mês
- **Notificações:** Telegram Bot API
- **Backtesting cripto:** Freqtrade (built-in, dados históricos via exchange API)
- **Backtesting B3/Forex:** MT5 Strategy Tester integrado

### Architecture Considerations

- **Repository Structure:** Monorepo — backend Python + frontend Next.js no mesmo repositório
- **Service Architecture:** Microsserviços simples — engine de trading (Python), API REST (FastAPI), dashboard (Next.js), scheduler de tarefas (Celery + Redis)
- **Integration Requirements:** APIs REST e WebSocket das exchanges (Binance, B3, Forex broker); Telegram Bot API
- **Security/Compliance:** API keys das exchanges armazenadas criptografadas (nunca em texto plano); sem permissão de saque nas API keys (apenas trade); comunicação via HTTPS/WSS; autenticação local no dashboard

---

## Constraints & Assumptions

### Constraints

- **Budget:** Projeto pessoal — infraestrutura deve ser de baixo custo (VPS ~$20-50/mês)
- **Timeline:** MVP funcional em 4–6 meses de desenvolvimento
- **Resources:** Desenvolvedor solo (o próprio owner)
- **Técnico:** B3 será acessada via MetaTrader 5 Python API (lib oficial) — requer conta em corretora parceira MT5 (XP ou Modal Mais, ambas gratuitas). MT5 roda nativo no Windows; no Linux requer Wine ou VM Windows.

### Key Assumptions

- O owner tem conhecimento básico de Python e desenvolvimento web
- Existe capital disponível para testes reais (mesmo que pequeno — ex: R$500–R$2.000 para início)
- As APIs das exchanges escolhidas têm documentação adequada e limites de rate suficientes para operação do robô
- O modelo ML inicial pode ser treinado com dados históricos públicos gratuitos (OHLCV de exchanges)
- Não há obrigações regulatórias impeditivas para uso pessoal de robô de trading no Brasil
- A latência da VPS escolhida será adequada para os mercados-alvo (não é HFT — high-frequency trading)

---

## Risks & Open Questions

### Key Risks

- **Risco de capital:** O robô pode executar operações ruins durante fase de aprendizado — *mitigação: paper trading obrigatório antes de ir ao vivo; capital inicial limitado; stop loss hard obrigatório*
- **Risco de API da B3:** Corretoras brasileiras têm APIs limitadas ou pagas — *mitigação: pesquisar corretoras com API aberta (Clear, XP Professional) ou iniciar com apenas cripto e Forex*
- **Overfitting do modelo ML:** Modelo que performa bem em backtesting mas falha no mercado real — *mitigação: validação walk-forward, out-of-sample testing, monitoramento contínuo de métricas*
- **Downtime do servidor:** VPS offline = robô parado em posição aberta — *mitigação: alertas de downtime, auto-restart de serviços, uso de stop loss na exchange (server-side orders)*
- **Mudança de regime de mercado:** Modelo treinado em bull market falha em bear market — *mitigação: treinar com dados de múltiplos regimes; detector de regime como feature do modelo*
- **Segurança das API keys:** Comprometimento de credenciais pode levar a perdas — *mitigação: API keys com permissão somente de trade (sem saque), criptografia em repouso, VPS com firewall*

### Open Questions

- Qual corretora brasileira oferece a melhor API para automação de ordens na B3? (Clear, XP, ou outra?)
- Freqtrade vs. implementação própria: usar framework de backtesting existente ou construir do zero para maior controle?
- Qual estratégia inicial de ML treinar primeiro: classificação de direção (alta/baixa) ou regressão de retorno esperado?
- Como lidar com custos de transação (spread, corretagem, slippage) no modelo de ML?
- O capital inicial para testes será em cripto ou B3 primeiro?

### Areas Needing Further Research

- ~~Disponibilidade e custo das APIs de corretoras B3~~ **RESOLVIDO:** MT5 Python API via XP ou Modal Mais (gratuito)
- ~~Comparativo de frameworks~~ **RESOLVIDO:** Freqtrade (cripto) + MT5 Python (B3/Forex)
- Requisitos regulatórios para uso de robô de trading por pessoa física no Brasil (CVM) — verificar se há obrigação de registro
- Fontes de dados históricos gratuitos para B3 (OHLCV mini-índice/mini-dólar) — verificar Yahoo Finance, investing.com ou MT5 histórico próprio
- Latência típica de VPS Windows no Brasil (Locaweb, KingHost, DigitalOcean SP) vs. servidores da Binance e B3

---

## Appendices

### A. Research Summary

**Mercado de Trading Bots (pesquisa realizada em 28/04/2026):**

| Plataforma | Mercado | Foco de IA | Multi-mercado | Preço |
|---|---|---|---|---|
| 3Commas | Cripto | Limitado (AI Grid Bot) | Não | $29–$99/mês |
| Cryptohopper | Cripto | Strategy Designer básico | Não | $19–$99/mês |
| MetaTrader 5 | Forex/Ações | Nenhum nativo | Limitado | Gratuito |
| Profit Pro | B3 | Nenhum | Não | $50+/mês |
| **autoxtrade** | **Cripto+B3+Forex** | **ML adaptativo** | **Sim** | **Pessoal** |

**Gap identificado:** Nenhuma plataforma existente combina os três mercados com IA/ML adaptativa real em uma solução pessoal de baixo custo.

### B. Stakeholder Input

Projeto pessoal — decisões concentradas no owner/desenvolvedor.

### C. References

- [3Commas](https://3commas.io) — Referência de funcionalidades e UX
- [Cryptohopper](https://cryptohopper.com/features) — Referência de features de trading bot
- [CCXT Library](https://github.com/ccxt/ccxt) — Biblioteca Python multi-exchange
- [Freqtrade](https://www.freqtrade.io) — Framework Python de trading bot com ML
- [Backtrader](https://www.backtrader.com) — Framework de backtesting Python
- [Alpaca Markets API](https://alpaca.markets) — Alternativa para mercado US se B3 API for inacessível

---

## Next Steps

1. Revisar e aprovar este Project Brief
2. ~~Pesquisar corretora B3~~ **RESOLVIDO:** abrir conta na XP ou Modal Mais (gratuito) e ativar MT5
3. ~~Decidir framework~~ **RESOLVIDO:** Freqtrade (cripto fase 1) + MT5 Python API (B3/Forex fase 1b)
4. **Estratégia MVP recomendada:** Iniciar SOMENTE com Binance/cripto (usando Freqtrade) → validar ML e sistema → adicionar MT5 (B3+Forex) após 30 dias de operação real estável
5. Verificar aspectos regulatórios CVM para uso de robô por PF no Brasil
6. Iniciar criação do PRD com o agente PM (John)

---

### PM Handoff

Este Project Brief fornece o contexto completo para o **autoxtrade**. Por favor, inicie no modo de geração de PRD, revise este brief minuciosamente e trabalhe com o usuário para criar o PRD seção por seção, solicitando esclarecimentos quando necessário e sugerindo melhorias onde aplicável.
