# Histórico MT5 de EURUSD — coleta e replay

Executado em 30/10/2026. O MT5 conectado era `EquitiBrokerageSC-Live`; os pedidos enviados foram exclusivamente `GET_HISTORIC_DATA` por meio da whitelist de market data. Nenhuma ordem, alteração, cancelamento ou operação de posição foi solicitada.

## Coleta

- Símbolo/timeframe: `EURUSD.pr`, H1.
- Janela solicitada: 30/10/2024 a 31/10/2026, dividida em 25 blocos para evitar resposta grande.
- Respostas: 25/25 recebidas; o arquivo `DWX_Messages.txt` confirmou `Successfully read historic data for EURUSD.pr_H1`.
- Candles únicos: 12.452; intervalo nos timestamps emitidos pelo servidor: `2024.10.30 00:00` a `2026.10.30 19:00`.
- Validação: 0 valores OHLC inválidos, sequência cronológica e sem timestamps duplicados.
- Snapshot bruto preservado em [1h_mt5_server_time.json](../../backend/ml/data/FOREX/EURUSD.pr/1h_mt5_server_time.json).

Na coleta, os timestamps foram primeiro preservados sem conversão no arquivo bruto. A página da Equiti descreve o horário MT5 como EET, UTC+2 no inverno e UTC+3 durante o horário de verão ([horário MT5 da Equiti](https://www.equiti.com/sc-en/holiday-hours/)); essa regra foi então aplicada ao Parquet runtime, mantendo o original como artefato de auditoria.

### Importação e qualificação após normalização

O parser DWX, o coletor MT5, o piloto paper e o replay agora convertem timestamps textuais pela zona configurável `MT5_SERVER_TIMEZONE` (padrão `Europe/Athens`) e mantêm valores numéricos como Unix UTC. O bruto permanece preservado acima; o Parquet runtime `backend/ml/data/FOREX/EURUSD.pr/1h.parquet` foi atualizado com as 12.452 barras UTC. O Parquet anterior foi preservado como `1h.pre-mt5-20261030.parquet`. A qualificação usa somente barras fechadas: 12.451, com fim em `2026-10-30T16:00:00+00:00` e idade de 0,04 dias. O carregador runtime devolveu 300/300 barras recentes e idade de 121,5 segundos na última verificação.

O relógio exibido no screenshot do terminal parece diferir em uma hora da zona publicada pela Equiti para essa data. Por isso a conversão sazonal ainda deve ser comparada com o relógio do servidor no terminal antes de usar hora do dia para filtros; a diferença não altera a sequência/preço do replay. A qualificação abaixo usa candles fechados e os timestamps EET/EEST convertidos.

## Replay diagnóstico

Usado o setup registrado no diagnóstico público recente (ADX 23, alvo 2R, safe break 1R, saída automática habilitada), em dois blocos cronológicos finais. Custos meramente ilustrativos: meia-spread 0,00005, comissão 0,00007 do notional por lado e slippage 0,00002 por lado. Não representam a tabela confirmada da conta.

| Fold | Trades | Win rate líquido | Profit factor líquido | Expectativa líquida/trade | Drawdown máximo | Resultado |
|---|---:|---:|---:|---:|---:|---|
| 1 | 39 | 35,90% | 0,4765 | -0,059258% | 2,6741% | Reprovado |
| 2 | 53 | 28,30% | 0,5954 | -0,054445% | 3,6080% | Reprovado |

Ambos os folds superaram o mínimo de 30 trades, mas falharam win rate, profit factor e expectativa. Este replay não é qualificação: custos da conta não foram confirmados e os timestamps ainda exigem conversão validada. A flag paper permanece fechada; não ajustei parâmetros com base nesses folds.
