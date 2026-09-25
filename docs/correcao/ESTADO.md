# Correção AutoXTrade — estado das etapas

## Atualização etapa 03 — 2026-09-25

Etapa 03 implementada e validada no checkout macOS com `backend/.venv/bin/python`.

- F03 corrigido: proteção DWX/MT5 usa modificação identificada da posição (`MODIFY_ORDER` via `modify_position_protection`) e os testes provam que não há fallback para ordem oposta por `place_order`.
- F09 corrigido: reconciliação read-only compara trades abertos com posições/ordens reportadas pelo conector, sinaliza órfãos/divergências e não executa fechamento, cancelamento, redução ou abertura real.
- `backend/trading/safety.py` permaneceu intacto; o gate segue fail-closed para execução externa.
- Pendências mantidas e documentadas: F04 / etapa 04, F05 / etapa 06 e F12 / etapa 05.
- Validação final da etapa 03: suíte completa `116 passed / 3 xfailed`; `test_safety.py` `31 passed`; `test_dwx_protection.py` `1 passed`; `test_reconciliation.py` `5 passed`; `--runxfail` reproduziu apenas F04/F05.
- Remoto deste checkout: `git@github.com:sjtecnologia/autoxtrade.git`.

# Correção AutoXTrade — etapa 01

Data: 2026-09-22. **Etapa 01 implementada; suíte completa não aprovada.**
Etapas 02–09 são dependências futuras, não executadas nesta entrega.

## Limites e preservação

- Não foi encontrado AGENTS.md no projeto nem nos diretórios ancestrais consultados.
- A cópia recebida não tem `.git`: `git status --short` retornou “not a git repository”. Não há identificação verificável de commit ou de alterações anteriores do usuário. O estado inicial dos fontes foi preservado em `.correcao-runtime/baseline/backend`; o diff textual desta entrega está em `etapa01.diff`.
- Nenhum `.env` foi lido para esta análise, nenhum modelo recebido foi desserializado e o `backend/.venv` recebido não foi executado. Arquivos de configuração inspecionados: fontes, Docker/Compose e `.env.example`.
- Não foram iniciados API, Celery, MT5, PostgreSQL, Redis ou Docker Compose. Sem corretora, ordens reais, Telegram, commit, push, deploy ou alteração de banco/volumes. Rede usada somente para obter ferramentas/dependências públicas de teste.
- Testes exercitam simuladores, mocks e arquivos DWX temporários. Não provam integração financeira, proteção real ou rentabilidade.

## Arquitetura e versões verificadas

Backend Python/FastAPI, SQLAlchemy async/asyncpg e PostgreSQL; Redis para cache/pubsub/aprovações; Celery worker e beat. `requirements.txt` contém limites mínimos, sem lock. Docker declara Python 3.11, PostgreSQL 16-alpine, Redis 7-alpine; migração única `001_initial_schema.py` e Alembic com execução online async. Não foi aplicada migração.

Frontend Next.js App Router: páginas dashboard, approvals, charts, history, models e settings; React Query, Zustand e WebSocket nativo. Manifesto fixa Next 14.2.3; dependências locais consultadas: Next 14.2.3, React 18.3.1, TypeScript 5.9.3. Node local 24.14.0; Docker frontend usa Node 20. `socket.io-client` está declarado, mas o hook usa `new WebSocket`. Nginx encaminha API e WS; TLS depende dos certificados montados. Docker backend não executa migrações no CMD; healthcheck chama curl, não instalado explicitamente no Dockerfile. Estes serviços não foram construídos nem iniciados.

Auth REST: `api/auth.py::verify_token`, Bearer estático com `compare_digest`; routes de mercado não têm essa dependência. `/ws` aceita sem autenticação. Frontend obtém token de `NEXT_PUBLIC_API_TOKEN`; Dockerfile passa apenas URLs como build args. Isso exige revisão de exposição/configuração na etapa 08, sem transformar o projeto em multiusuário.

Dados persistidos: `Trade`, `TradeEvent`, `BotConfig`, `MLModel`, `EquitySnapshot`, `ConfigAuditLog`. O banco armazena mercado/modo/IDs de ordem, mas não um contrato de identidade da conta, netting/hedging, fills parciais ou reconciliação por ticket. `Trade.mode` tem default live; `BotConfig.mode` tem default paper. Não se inferiu estado do banco real a partir desses defaults.

## Cadeia realmente conectada nos fontes

| Passo | Chamadores e comportamento observado | Limite atual |
|---|---|---|
| Coleta | Beat `update_market_data` diário → `DataCollector.update_incremental` ou `MT5DataCollector` → parquet. API de mercado lê parquet/DWX e tem fallbacks sintéticos. | Worker cripto usa BTC/ETH fixos; B3/FOREX usa `cfg.symbols`, inexistente no modelo (`enabled_symbols`). Dados de mercados pausados deixam de atualizar pelo job. |
| Sinal | Beat `scan_and_trade` a cada 15 min → configs ativas/horário → `_process_market`. Default DiDi manual: `_load_ohlcv` → `evaluate_entry` com mínimo 80 linhas. | `_process_ml_entries` carrega/prediz e apenas registra log; não envia ordens. `_demo_trade_plan` não é chamado em produção. |
| Risco inicial | DiDi → `RiskManager.can_open_position`: ativo/pausado, limite de trades, correlação. | Não usa cálculo de drawdown injetado nem `PositionSizer.calculate`: fabrica quantidade 1 e risco 100. |
| Aprovação | `EntryApprovalService.create_pending_entry` → Redis TTL → notificação/gráfico → frontend approvals → POST approve (permite sobrescrever quantidade). | Atualização Redis não atômica; duplicidade entre workers é possível. Nesta etapa live é recusado no serviço e retorna 409 na rota. |
| Execução aprovada | Beat 30s → `pop_approved_entries` → `_process_entry_approvals_async` → verifica trade aberto → PaperTrader ou OrderExecutor. | Agora revalida pausa/modo vigente e bloqueia live antes do conector. Não revalida preço, sizing e orçamento completos no instante do envio. |
| Envio | `OrderExecutor.open_position` → `place_order` → persiste Trade/Event. DWX envia OPEN_ORDER; Binance usa CCXT. | Bloqueio central também no executor, nos métodos diretos e no writer DWX. Factory default paper usa apenas SimulatedConnector. |
| Confirmação | DWX aguarda evento e retorna primeiro ticket do símbolo; timeout pode produzir id `pending`. Executor usa average ou preço previsto. | Não comprova correspondência do pedido, fill integral ou ausência de duplicação. Corrigir na etapa 02. |
| Proteção | Executor chama OCO, depois stop-market, depois fallback de emergência. | DWX OCO é uma nova ordem contrária; fallback finaliza só no banco. Quarentenado, não corrigido financeiramente. |
| Monitoramento | Beat 60s → `_monitor_open_positions_async` seleciona Trade aberto sem depender de BotConfig ativo → avalia safe break/saída → alertas; auto-close default false. | Safe break altera stop apenas no banco. Nesta etapa fechamento real é explicitamente recusado e continua o loop; teste garante que outro trade paper continua sendo gerido. |
| Fechamento | PaperTrader `_close_position`; OrderExecutor `close_position` existe e é chamado pelo monitor no fluxo anterior. | Não há rota REST manual de abertura/fechamento nesta versão: ação manual efetiva é aprovação. Close real fica bloqueado, sem resolução acidental para paper. |
| Reconciliação | `OrderMonitor.check_orphan_positions` e `handle_order_update` existem. | Não instanciados pelo lifespan ou tarefas. Não há reconciliação periódica financeira validada; os testes não simulam que exista. |
| P&L | Executor/PaperTrader calculam resultado → banco → `/trades/history`, stats daily/performance, `/risk/equity-history` → dashboard/relatórios. | Fee fixo, sem fills/taxas/câmbio/contratos reais. Snapshot usa saldo Binance e open_pnl zero; métricas não segregam sempre modo/conta. |

Outros componentes presentes sem ligação completa: `MarketDataCache` não é instanciado; `BinanceConnector.start_price_stream` não é iniciado no lifespan/worker; `PaperTrader.check_stops` não tem chamador; `PositionSizer` é instanciado com sessão onde espera conector, mas o cálculo é ignorado pelo manager. `OrderMonitor.startup` não é chamado. `ModelValidator` é chamado pelo trainer no walk-forward, porém o perfil DiDi ignora seus gates de performance; isso não equivale a não calcular métricas. `ModelPredictor` usa joblib.load em artefatos existentes — não executado nesta auditoria.

Celery também agenda: equity hourly, drawdown 5 min (pausa BotConfig), relatório 23h, retrain domingo 02h. Pausar novas entradas não cancela o monitor nem o job de drawdown. A coleta/snapshot que filtram ativos e a ausência de reconciliação permanecem pendências explícitas: não se promete gestão financeira operacional apenas porque o monitor continua agendado.

## Implementado nesta etapa

- `trading/safety.py`: natureza SIMULATED/DEMO/REAL/UNKNOWN e política central fail-closed. Configuração `REAL_NEW_EXPOSURE_ENABLED=false`; mesmo true não certifica capacidades inexistentes. DEMO não libera automaticamente execução; UNKNOWN nunca é convertido em demo.
- Entradas bloqueadas no executor, serviço/rota de aprovação, consumo da fila e métodos Binance/DWX; comandos DWX diretos de mutação passam pelo bloqueio. Não basta mudar configuração do frontend ou usar chamada direta.
- Factory inclui modo na chave de cache, valida mercado/modo, default paper e adapter inteiramente em memória. Corrigida incompatibilidade de construtor Binance no ramo reservado. Imports de conectores são tardios para paper não importar Binance.
- Gestão tem autorização separada de entradas. Simulação pode cancelar/proteger com entrada real bloqueada. Gestão mutante real permanece indisponível: é necessário contrato validado por ticket/posição. Monitor continua independente da pausa e não fecha posições reais nesta execução.
- Testes instalam isolamento antes da coleta: segredos substituídos, dotenv desabilitado, rede/DNS externa negados, Telegram transport negado, DWX somente no diretório temporário, modelos recebidos negados. Tentativas capturadas pela aplicação também fazem a sessão de teste falhar. Por padrão nem localhost é liberado; `AUTOXTRADE_TEST_ENDPOINTS=127.0.0.1:porta` permite somente serviços explicitamente de teste. Nenhum foi permitido nesta execução. Exceções técnicas restritas: socketpair local de wakeup do asyncio e `git rev-parse --git-dir` usado pelo versionamento interno de CCXT.
- Regressões estritas para quatro defeitos ainda abertos; regressão da factory passa após a contenção. Teste legado de executor recebeu apenas identificação explícita de seu mock como simulado, sem alteração de asserts.

Diff de implementação: [etapa01.diff](etapa01.diff), com 19 arquivos de código/configuração/testes; [arquivos-alterados.txt](arquivos-alterados.txt) registra SHA256 do estado entregue. Os documentos e logs deste diretório são novos artefatos de auditoria e não estão embutidos nesse diff. Leitura sintática via AST dos 79 fontes Python do backend passou sem executar os módulos. Nenhum arquivo frontend ou migração foi alterado.

## Testes, comandos e resultados

Runtime novo `.correcao-runtime/python/python.exe`: Python 3.11.9 embedded oficial (Python 3.11 do Docker), sem usar venv recebido. `py -0p`: nenhum Python instalado registrado. Docker daemon indisponível; WSL não acessível no sandbox. Download exigiu execução fora do sandbox por falha TLS local; aprovado automaticamente, limitado a ferramentas públicas.

Dependências diretas testadas: mínimos de `backend/requirements.txt`, trocando somente CCXT 4.3.0 (não publicado) por 4.3.1. `pandas-ta==0.3.14b0` indisponível no índice para Python 3.11; versões oferecidas exigem >=3.12. Não substituído por pacote alternativo nem atualizado todo Python/stack. Isso explica 11 falhas de features. O lock do ambiente completo resolvido está em [dependencias-testadas.txt](dependencias-testadas.txt); não substitui os requisitos de produção. Bootstrap pip resolveu suas ferramentas; dependências transitivas foram resolvidas pelo pip e estão registradas nesse arquivo.

Comandos, a partir da raiz (PowerShell; `$LASTEXITCODE` é o resultado de pytest):

```powershell
# Recriar um venv NOVO em máquina com Python 3.11, sem usar backend/.venv
py -3.11 -m venv .correcao-runtime/venv
.correcao-runtime/venv/Scripts/python.exe -m pip install -r docs/correcao/dependencias-testadas.txt

# Nesta execução foi usado o runtime embedded:
./.correcao-runtime/python/python.exe -m pytest backend/tests -q -rx --tb=short
./.correcao-runtime/python/python.exe -m pytest backend/tests/unit/test_safety.py -q --tb=short
./.correcao-runtime/python/python.exe -m pytest backend/tests/unit/test_regressions_pending.py --runxfail -q --tb=short
./.correcao-runtime/python/python.exe -m pytest backend/tests/isolation_canaries.py -q --tb=short
node frontend/node_modules/typescript/bin/tsc --noEmit --incremental false -p frontend/tsconfig.json
```

| Verificação | Resultado real | Evidência |
|---|---|---|
| Base original, com harness de isolamento | **63 passed / 14 failed**, exit 1 | [testes-base.txt](testes-base.txt) |
| Suíte após etapa 01 | **95 passed / 14 failed / 4 xfailed**, exit 1 | [testes-etapa01.txt](testes-etapa01.txt) |
| Bloqueio/pausa/simulação | **31 passed**, exit 0 | [testes-bloqueio.txt](testes-bloqueio.txt) |
| Regressões com `--runxfail` | **4 failed / 1 passed**, exit 1 deliberadamente | [regressoes-abertas.txt](regressoes-abertas.txt) |
| Sondas de isolamento explícitas | **3 erros de teardown esperados**, exit 1; tentativas bloqueadas antes de efeitos externos | [testes-isolamento.txt](testes-isolamento.txt) |
| Frontend typecheck | exit 0, sem saída | [frontend-typecheck.txt](frontend-typecheck.txt) |

Base executada sobre a cópia pré-mudança, com o mesmo harness: `python -c "import sys, pytest; sys.path.insert(0, '.correcao-runtime/baseline/backend'); raise SystemExit(pytest.main(['.correcao-runtime/baseline/backend/tests','-q','--tb=short']))"`. Tentativa inicial ocorreu antes de terminar instalação; repetida após concluir dependências. Ajustado isolamento para socketpair Windows e consulta local de versão do CCXT antes da medição final.

As 14 falhas anteriores continuam visíveis: 11 por pandas-ta ausente, `test_detecta_gap_pequeno` (gap não preenchido), `test_close_position` (fixture SimpleNamespace incompatível com relacionamento SQLAlchemy), `test_rejeita_step_size_zero` (regex minúscula versus mensagem `Step size`). Não foram modificados asserts para aceitá-las. Warning Pydantic `model_version`/namespace protegido permanece.

## Etapas e pendências

| Etapa | Escopo de acompanhamento | Estado / aceitação |
|---|---|---|
| 01 | Inventário, contenção e base reproduzível | Implementada e testada localmente; suíte geral reprovada, corretoras bloqueadas. |
| 02 | Envio, confirmação, idempotência e fechamento | Implementada e validada antes da etapa 03; contrato seguro preservado. |
| 03 | Proteção MT5 e reconciliação | Implementada em 2026-09-25: proteção modifica posição/ticket sem abrir lado oposto; reconciliação read-only sinaliza órfãos/divergências. F03/F09 corrigidos. |
| 04 | Risco e dimensionamento | Pendente: saldo/risco/distância/contrato/step e drawdown reais; revalidar aprovação. F04/F10. |
| 05 | Dados e continuidade | Pendente: timestamps/frescor/gaps, símbolos válidos, remover ambiguidade de sintéticos; coleta de posições pausadas. F11/F12. |
| 06 | Validação ML/DiDi | Pendente: rejeitar métricas ruins, integridade de artefatos e dependência pandas-ta. F05/F13. |
| 07 | Reconciliação contábil/P&L | Pendente: fills/taxas/moeda/modo/conta, equity com posições abertas. F14. |
| 08 | API/frontend/auth/deploy | Pendente: WS/auth, transparência de estado, migração e smoke local de containers. F15/F16. |
| 09 | Validação final de contratos | Pendente: suíte verde, simuladores de falhas e integração autorizada futura por conta; sem promessa de rentabilidade. |

Dependência exata para liberar conectores: evidência de identidade e natureza da conta, protocolo/versão DWX/EA, netting ou hedging, símbolo/contrato/tick/lot/step/moeda, mapeamento idempotente pedido→ticket→fills, modificação SL/TP e redução/fechamento por posição, cancelamento distinto de fechamento, reconciliação e taxas. Nenhum desses pontos é validado por uma flag ou por saldo disponível. Na ausência dessa evidência o bloqueio permanece. **Parada na etapa 01; próxima etapa 02.**
