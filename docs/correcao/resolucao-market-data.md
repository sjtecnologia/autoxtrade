# Resolução do market data DWX - etapa 08b

Data da investigação: 2026-09-25
Modo: somente leitura; nenhuma escrita foi feita no share.

## Estado observado

- Diretório: `/Volumes/MT5Files/DWX`
- Share: `smb://192.168.0.52/MT5Files`
- Arquivos presentes: `DWX_Orders.txt` (184 bytes), `DWX_Market_Data.txt` (4 bytes), `MarketWatchSymbols.txt` (204 bytes), `_TESTE_AUTOXTRADE.txt` (55 bytes).
- `DWX_Market_Data.txt` contém `{}` e continua vazio.
- `_TESTE_AUTOXTRADE.txt` contém somente `MARCADOR criado pelo autoxtrade em 18/08/2026 10:07:08`; não é pista de sintaxe.
- Não existem `DWX_Commands_*`, `DWX_Commands.txt`, `DWX_Historic_Data.txt` ou source/executável do EA no share inspecionado.

## Canal e sintaxe

A documentação local do projeto descreve somente a variante oficial do DWX Connect:

- Canal: `DWX_Commands_N.txt`, com `N` entre 0 e 49.
- Formato: `<:id|COMANDO|conteúdo:>`.
- Comando candidato para candles no conector atual: `GET_HISTORIC_DATA`.
- O código local monta o conteúdo no formato `símbolo,timeframe,start_ts,end_ts`.

Isso não confirma que a variante do EA instalada no servidor leia `DWX_Commands_N.txt`. O servidor usa nomes sem sufixo nos arquivos de estado, e nenhum arquivo de comando está presente para confirmar o padrão. A pasta do EA não está exposta no share; o único `.mq5` local é o exportador de `MarketWatchSymbols.txt`, não o servidor DWX.

## Resultado

Nenhum comando foi tentado. Não foi possível validar com segurança o nome exato do canal nem a sintaxe da variante instalada. O arquivo `DWX_Market_Data.txt` permanece no estado original `{}`; não há contagem de candles recebidos.

## Rechecagem 08b

Às 25/09, uma nova inspeção recursiva de `/Volumes/MT5Files` confirmou que `Temp/` está vazio e que `DWX/` continua contendo somente `_TESTE_AUTOXTRADE.txt`, `DWX_Market_Data.txt`, `DWX_Orders.txt` e `MarketWatchSymbols.txt`. Não há `DWX_Commands.txt`, `DWX_Commands_0.txt`, qualquer `DWX_Commands_*`, source/executável do EA ou diário de configuração disponível no share. O marcador permanece apenas um texto de identificação, sem formato de comando.

Comando tentado: **nenhum**. A única escrita permitida não foi executada porque o canal exato continua não verificável; assim, `DWX_Market_Data.txt` permanece `{}` e nenhuma hipótese foi testada por tentativa cega.

Hipóteses para investigação no host MT5:

1. O EA instalado usa `DWX_Commands_N.txt`, mas não está anexado/ativo no gráfico ou não está respondendo.
2. A variante instalada usa `DWX_Commands.txt` ou outro nome sem slot.
3. O módulo/comando de market data histórico está desabilitado ou o símbolo/timeframe não é suportado.
4. O diretório compartilhado não é a pasta `MQL5/Files/DWX` efetivamente usada pelo EA.

Próximo passo controlado: obter no host Windows o nome do `.ex5/.mq5` anexado e a configuração/diário do EA, ou um exemplo de arquivo de comando autorizado. Somente depois disso repetir um único pedido para `EURUSD.pr` em `M5`, com até 100 candles.

## Garantia de segurança

- ZERO comandos `DWX_Commands_*` escritos.
- ZERO ordens, cancelamentos, modificações ou fechamentos.
- `backend/trading/safety.py` não foi alterado.

## Retomada 08b - sintaxe confirmada do EA

O código do EA confirmou o canal `DWX_Commands_0.txt`. O wrapper whitelist foi criado em `backend/integration/market_data_request.py`; ele escreve ASCII sem newline, exige terminador `:>`, recusa comandos de ordem antes de escrever e aguarda o arquivo desaparecer.

### Pedido 1 - ticks

Comando exato:

`<:10001|SUBSCRIBE_SYMBOLS|EURUSD.pr:>`

Resultado: consumido pelo EA. `DWX_Market_Data.txt` passou de `{}` para:

`{"EURUSD.pr": {"bid": 1.13890, "ask": 1.13891, "last": 0.00000, "tick_value": 1.00000}}`

`DWX_Messages.txt` confirmou `Successfully subscribed to: EURUSD.pr`. O símbolo pertence ao `MarketWatchSymbols.txt` e o arquivo foi atualizado após o pedido; bid/ask foram aceitos como tick válido, sem inventar conteúdo.

### Pedido 2 - barra M5

Comando exato:

`<:10002|SUBSCRIBE_SYMBOLS_BAR_DATA|EURUSD.pr,M5:>`

Resultado: consumido pelo EA. `DWX_Bar_Data.txt` contém `EURUSD.pr_M5` com amostra `time=2026.09.25 20:15`, `open=1.139010`, `high=1.139050`, `low=1.138890`, `close=1.138960`, `tick_volume=216`. A mensagem do EA confirmou a inscrição de bar data.

### Pedido 3 - histórico M5

Janela usada: `start=1790294400`, `end=1790356855` (hoje UTC).

Comando exato:

`<:10003|GET_HISTORIC_DATA|EURUSD.pr,M5,1790294400,1790356855:>`

Resultado: consumido pelo EA. `DWX_Historic_Data.txt` contém a chave `EURUSD.pr_M5` e 208 candles. Validação etapa 05: JSON válido, símbolo suportado, timestamps em ordem cronológica e último timestamp `2026-09-25T17:20:00+00:00`.

### Operação futura

Recomenda-se refresh periódico de `SUBSCRIBE_SYMBOLS`/`SUBSCRIBE_SYMBOLS_BAR_DATA` com debounce por símbolo/timeframe, sem reescrever enquanto `DWX_Commands_0.txt` existir. Após cada resposta, aplicar a janela de frescor, ordem cronológica, gaps e validação de símbolo da etapa 05. Comandos de ordem continuam fora da whitelist e bloqueados.

Os três comandos foram os únicos arquivos escritos nesta execução. `DWX_Commands_0.txt` foi consumido em cada caso; nenhuma ordem, cancelamento, proteção ou modificação foi enviada.
