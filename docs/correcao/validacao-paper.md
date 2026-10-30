# Qualificação do paper trading

## Estado atual

Nenhum símbolo está habilitado. `PAPER_NEW_EXPOSURE_ENABLED` continua `false`; além disso, não há modelo de custos do broker em `.env` nem relatório aprovado em `backend/paper/qualification.json`.

### Reavaliação em 30/10/2026

Foi executado um replay diagnóstico de EURUSD H1 com candles públicos agregados recentes. O resultado completo está em [public-fx-replay-20261030.json](public-fx-replay-20261030.json). O feed e os custos são ilustrativos, portanto este relatório não habilita paper; serve apenas para verificar se a estratégia merece avançar para um teste com dados da corretora.

- Dados: 12.317 candles entre 30/10/2024 e 30/10/2026; último candle fechado em 15:00 UTC.
- Fold 1: 48 trades, win rate líquido 29,17%, PF 0,5155, expectativa -0,063919% por trade.
- Fold 2: 43 trades, win rate líquido 30,23%, PF 0,6848, expectativa -0,044398% por trade.
- Ambos os folds falharam win rate, profit factor e expectativa; nenhum símbolo foi qualificado.
- O candle recente satisfaz a janela de idade, mas a origem pública agregada e os custos simulados não representam a conta pretendida.

Também verifiquei os parquets locais: o mais recente termina em 30/07/2026, fora da janela de 30 dias em 30/10/2026. Não são dados suficientes para qualificação operacional.

Decisão: manter novas entradas paper e live bloqueadas. Não alterei os parâmetros para otimizar contra estes folds: ambos perderam, e ajustar com base neles seria seleção sobre o próprio período de validação. A estratégia atual precisa ser revista e depois avaliada em dados e custos broker-originados antes de novo piloto.

### Histórico MT5 recebido em 30/10/2026

O MT5 Live retornou 12.452 candles H1 de EURUSD em 25 pedidos `GET_HISTORIC_DATA`, sem comandos de negociação. A validação OHLC/ordem/duplicatas passou. O replay com custos apenas ilustrativos também reprovou os dois folds: PF 0,4705 e 0,5954; expectativa -0,060696% e -0,054445% por trade. Detalhes em [mt5-historic-validation-20261030.md](mt5-historic-validation-20261030.md).

O bruto está preservado em `backend/ml/data/FOREX/EURUSD.pr/1h_mt5_server_time.json`; o Parquet operacional foi atualizado após conversão sazonal EET/EEST e o anterior ficou como backup. A qualificação foi refeita só com candles fechados e continua reprovada. Custos da conta não foram confirmados. A estratégia segue reprovada e entradas paper permanecem bloqueadas.

A triagem de nove séries H1 locais está em [qualificacao-paper-inicial.json](qualificacao-paper-inicial.json). Todas terminam em julho de 2026, têm origem/corretora não documentada, e por isso estão vencidas para a regra de 30 dias. Os blocos finais também não chegaram a 30 operações por fold. As métricas sem custos são apenas diagnóstico; não são candidatas a habilitação.

Para reproduzir a triagem pública recente, use `python -m paper.public_fx_replay --period 2y` a partir de `backend/`. Essa fonte é agregada e possivelmente atrasada, e os custos padrão são hipóteses; o relatório é sempre `diagnostic_only` e o runtime o rejeita para liberar entradas. A qualificação operacional exige dados e custos correspondentes à conta/broker usada no paper.

## Premissas do qualificador

O comando avalia a estratégia DiDi atual sem ajuste de parâmetros, em dois blocos cronológicos finais. Cada bloco precisa cumprir simultaneamente:

- pelo menos 30 operações;
- win rate líquido de pelo menos 48%;
- profit factor líquido de pelo menos 1,30;
- expectativa média líquida maior que 0,01% por trade;
- drawdown máximo de até 20%;
- dados com no máximo 30 dias desde o candle final.

Custos são obrigatórios por símbolo: `spread_price` e `slippage_price` são unidades de preço por lado; `commission_rate` é uma fração do notional por lado. O mesmo mapa de custos é usado pelo paper trader. Esses números precisam refletir o contrato e a tabela da corretora. O relatório também registra hash da estratégia, parâmetros e custos; qualquer divergência invalida a qualificação.

`commission_rate` representa comissão proporcional ao notional. Se a corretora cobrar uma taxa fixa por lote, converta-a para uma taxa equivalente usando o tamanho do contrato e o notional avaliado; enquanto essa equivalência não representar a conta com fidelidade, mantenha as entradas bloqueadas.

## Execução

1. Crie um arquivo de custos usando a especificação real da conta, por exemplo:

```json
{
  "EURUSD.pr": {
    "spread_price": "0.00005",
    "commission_rate": "0.00002",
    "slippage_price": "0.00001"
  }
}
```

Os números do exemplo são apenas ilustrativos: substitua-os pelos custos medidos da conta, nunca os use como cotação real.

2. Configure o mesmo conteúdo em `PAPER_COSTS_JSON` no `.env` do serviço.
3. Com histórico suficiente e recente, execute a partir de `backend/` para cada símbolo incluído no arquivo de custos:

```bash
python -m paper.qualification --symbol EURUSD.pr --timeframe 1h --parquet --costs-file /caminho/custos.json
```

O comando acumula os símbolos qualificados com o mesmo hash de estratégia e mapa de custos, e salva `backend/paper/qualification.json` por padrão. Mudança de código, parâmetros ou custos invalida resultados anteriores. A decisão de cada fold e os motivos de reprovação aparecem no terminal e no arquivo.
4. Só depois de um relatório aprovado, da revisão humana e do uso do mesmo histórico/contrato, `PAPER_NEW_EXPOSURE_ENABLED=true` permite tentar novas entradas paper. O próprio runtime ainda exige flag, relatório recente, hash/params atuais, custos idênticos e símbolo aprovado.

A flag só habilita simulação. A execução real continua bloqueada. Um relatório aprovado não promete lucro; serve como um gate mínimo de evidência histórica.
