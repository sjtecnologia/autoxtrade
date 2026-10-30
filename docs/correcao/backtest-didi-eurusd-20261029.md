# Ajustes e avaliação DiDi — EURUSD

Executado em 29/10/2026. Nenhuma operação foi enviada; o ledger paper e os arquivos DWX não foram modificados.

## Alterações

- `backend/tasks/trading_tasks.py`: o monitor agora verifica máxima/mínima do último candle e fecha posições `paper` quando SL ou TP é tocado. Se ambos aparecem tocados no mesmo OHLC, assume stop primeiro. Antes, `PaperTrader.check_stops()` existia, mas não tinha chamador no monitor.
- `backend/ml/trainer.py`: os gates de aprovação do perfil DiDi foram endurecidos: profit factor ≥ 1,30, win rate ≥ 48%, ao menos 30 operações, expectativa ≥ 0,01%, drawdown ≤ 25% e dispersão de PF ≤ 1,2. Uma configuração que tente desligar os gates agora rejeita o treino. Isso impede promover modelos com validação insuficiente; não altera o fluxo manual DiDi.
- `backend/paper/backtest.py`: replay offline reutilizável com indicador calculado uma vez, preenchimento na abertura seguinte, stops/alvos conservadores em gaps, fechamento de regra no candle fechado e comparação de custos. O filtro extra de Estocástico/TRIX fica opcional e desligado por padrão porque não mostrou melhora fora da amostra.

## Resultado do replay

### Histórico MT5 M5

- 208 candles de `EURUSD.pr`, 25/09/2026 00:05–17:20 UTC.
- 5 operações: 2 vencedoras (40%); profit factor bruto 0,5455; P&L bruto -0,000550 unidades de preço.
- Com custo ilustrativo de 1 pip por lado e sem comissão: 1/5 vencedora (20%), PF 0,225 e P&L -0,001550.

### Cache local H1, dividido cronologicamente 60/40

O cache local tem 3.030 barras de 04/02 a 30/07/2026. Sua origem/corretora não está registrada; os resultados são exploratórios e separados do histórico MT5.

| Configuração | Treino: trades / acerto / PF bruto / P&L | Período posterior: trades / acerto / PF bruto / P&L |
|---|---:|---:|
| Atual, ADX 23 e alvo 2R | 28 / 35,71% / 0,302 / -0,031916 | 18 / 22,22% / 0,610 / -0,010318 |
| Alvo curto 0,25R | 37 / 45,95% / 0,368 / -0,034993 | 22 / 40,91% / 0,551 / -0,012203 |
| ADX 30 e alvo 0,5R | 13 / 23,08% / 0,125 / -0,023568 | 4 / 50% / 1,621 / +0,001733 |

Com 1 pip por lado, sem comissão, a configuração atual ficou em 8/28 vencedoras no trecho inicial e 3/18 no posterior, com P&L negativo nos dois. O ajuste que chegou a 50% no trecho posterior teve só quatro operações e perdeu no treino; não é evidência para adotá-lo. Reduzir o alvo elevou a taxa de acerto, mas manteve PF abaixo de 1 e P&L negativo.

## Custos paper atuais

O `PaperTrader` ainda aplica spread de 0,1% por lado e comissão de 0,1% por lado, independentemente do mercado. Em EURUSD isso equivale a custos muito acima da hipótese ilustrativa de 1 pip por lado e faz até os alvos curtos simulados terminarem negativos. Não alterei esses custos sem especificação da corretora, tipo de conta, lote/contrato e comissão reais; eles precisam ser parametrizados por instrumento para que o P&L paper represente a execução pretendida.

## Conclusão operacional

Nenhuma configuração examinada teve desempenho consistente e positivo nos dois períodos. Por isso não elevei o ADX nem tornei o filtro opcional obrigatório: isso teria aparência de melhora apenas em amostras pequenas. O ajuste entregue reduz o risco de posições paper ignorarem SL/TP e bloqueia a aprovação de modelos DiDi fracos; ele não demonstra aumento de acerto para o fluxo manual, que continua sujeito à aprovação humana.

Verificação realizada: replay em dados locais, parse AST dos arquivos Python alterados e `git diff --check`. A suíte unitária não foi executada.

## Trava fail-closed de novas entradas

Após esta avaliação, `PAPER_NEW_EXPOSURE_ENABLED` foi adicionado com padrão `false`. Com o padrão atual, o scanner DiDi não cria novas aprovações; aprovações paper pendentes não podem ser aprovadas/executadas; e chamadas diretas a `PaperTrader` e `OrderExecutor` também são recusadas. A mesma condição é revalidada no instante de processar a fila. A execução live continua bloqueada pelas guardas preexistentes.

Para habilitar paper depois de uma validação rentável fora da amostra com custos reais do instrumento, configure explicitamente `PAPER_NEW_EXPOSURE_ENABLED=true` no ambiente do serviço e reinicie-o. Isso só habilita simulação; não libera ordens live. O `.env` local não foi alterado.
