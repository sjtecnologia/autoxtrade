# Replay paper com cotações públicas recentes

Executado em **2026-10-29T19:18:05.098826+00:00**. Fonte: [Yahoo Finance — moedas](https://finance.yahoo.com/markets/currencies/), feed agregado `CCY`, candles H1. A fonte avisa que os dados podem estar atrasados; cada preço é o valor mais recente fornecido na coleta. O último candle em formação foi excluído; barras até **2026-10-29 18:00:00+00:00 UTC**.

**Segurança:** replay offline somente. Nenhuma ordem foi enviada, nenhuma conta foi acessada e nenhum dinheiro foi usado. O feed público não é o feed da corretora do usuário.

**Custos simulados, não verificados na corretora:** meia-spread de 0,5–0,8 pip por lado, slippage de 0,2–0,3 pip por lado e comissão de 0,00007 do notional por lado. A fonte fornece OHLC e preço agregado, não os custos efetivos da conta.

Correções refletidas: stop e alvo preservam os valores calculados no candle do sinal; preenchimento na abertura seguinte; safe break só protege a partir do candle seguinte; saídas por sinal seguem `DIDI_EXIT_AUTO_CLOSE=false`, igual à configuração atual. Se SL e TP cabem no mesmo H1, assume-se o stop primeiro.

Critério: em cada um dos dois blocos cronológicos finais, pelo menos 30 trades, win rate líquido ≥48%, PF líquido ≥1,30, expectativa >0,01% por trade e drawdown ≤20%; histórico termina em até 30 dias.

| Par | Preço mais recente | Último H1 fechado | Trades OOS 1 / 2 | Acerto % 1 / 2 | PF 1 / 2 | Expectativa % 1 / 2 | Decisão |
|---|---:|---|---:|---:|---:|---:|---|
| EURUSD.pr | 1.1166 USD | 2026-10-29 18:00:00+00:00 | 47 / 44 | 29.79 / 29.55 | 0.5464 / 0.6403 | -0.0575 / -0.0525 | Reprovado |
| GBPUSD.pr | 1.3365 USD | 2026-10-29 18:00:00+00:00 | 26 / 46 | 30.77 / 39.13 | 0.5855 / 1.0534 | -0.0679 / 0.0057 | Reprovado |
| USDJPY.pr | 157.919 JPY | 2026-10-29 18:00:00+00:00 | 38 / 35 | 34.21 / 37.14 | 1.1213 / 0.7744 | 0.0165 / -0.0306 | Reprovado |
| AUDUSD.pr | 0.7034 USD | 2026-10-29 18:00:00+00:00 | 24 / 28 | 41.67 / 25.00 | 0.9488 / 0.7748 | -0.0106 / -0.0356 | Reprovado |
| NZDUSD.pr | 0.5632 USD | 2026-10-29 18:00:00+00:00 | 21 / 24 | 33.33 / 33.33 | 0.8835 / 1.3609 | -0.0211 / 0.0514 | Reprovado |
| USDCAD.pr | 1.3986 CAD | 2026-10-29 18:00:00+00:00 | 51 / 60 | 27.45 / 36.67 | 0.6086 / 0.7423 | -0.0376 / -0.0251 | Reprovado |
| USDCHF.pr | 0.8262 CHF | 2026-10-29 18:00:00+00:00 | 28 / 34 | 42.86 / 29.41 | 0.812 / 0.4423 | -0.0293 / -0.1084 | Reprovado |

## Resultado

**Qualificados: 0 de 7.** Foram usados entre 12249 e 12383 candles por par, mas nenhum cumpriu simultaneamente os critérios. Não reduzi os limites para fabricar aprovação.

Novas entradas paper continuam bloqueadas. Este relatório é diagnóstico e não pode liberar entradas. Para medir a conta, use histórico e custos do mesmo broker/feed do paper.

SHA-256 da implementação avaliada: `11df1376bdf19c90a401ba82cd82d0c9fbed90c39904b8cf02d63dc911ba1a08`.

Dados completos: [qualificacao-paper-live-20261029.json](qualificacao-paper-live-20261029.json). Para repetir o replay público: `python -m paper.public_fx_replay --period 2y` a partir de `backend/`; o resultado é sempre diagnóstico e não habilita operações.
