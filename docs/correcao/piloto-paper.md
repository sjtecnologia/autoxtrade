# Piloto paper-trading simulado

- Executado em: `2026-09-25T18:08:14.211278+00:00`
- Diretório DWX lido: `/Volumes/MT5Files/DWX`
- Iterações: 5
- Modo de dados: `real_mt5`
- Fonte de preço: `mt5_real`
- Modo de dados usado: `real_mt5`; fonte de preço: `mt5_real`.
- Símbolo avaliado: `EURUSD.pr`
- Sinais: 0
- Ordens simuladas gravadas: 0
- Ledger: `/Users/renato/Documents/Developement/autoxtrade 2/backend/paper/ledger.json`
- Saúde da fonte de posições: availability=`missing` (DWX_Positions_* ausente; posições não são inferidas como zero)
- Divergências/órfãos: 0

## Sinais

- iteração 1: signal=none; data=real_mt5; tick={'bid': 1.1391, 'ask': 1.13911, 'last': 0.0, 'tick_value': 1.0}; candle={'time': '2026.09.25 21:00', 'open': 1.13911, 'high': 1.1392, 'low': 1.13906, 'close': 1.13906, 'tick_volume': 147}
- iteração 2: signal=none; data=real_mt5; tick={'bid': 1.13911, 'ask': 1.13912, 'last': 0.0, 'tick_value': 1.0}; candle={'time': '2026.09.25 21:00', 'open': 1.13911, 'high': 1.1392, 'low': 1.13906, 'close': 1.13906, 'tick_volume': 147}
- iteração 3: signal=none; data=real_mt5; tick={'bid': 1.13911, 'ask': 1.13912, 'last': 0.0, 'tick_value': 1.0}; candle={'time': '2026.09.25 21:00', 'open': 1.13911, 'high': 1.1392, 'low': 1.13906, 'close': 1.13906, 'tick_volume': 147}
- iteração 4: signal=none; data=real_mt5; tick={'bid': 1.13911, 'ask': 1.13912, 'last': 0.0, 'tick_value': 1.0}; candle={'time': '2026.09.25 21:00', 'open': 1.13911, 'high': 1.1392, 'low': 1.13906, 'close': 1.13906, 'tick_volume': 147}
- iteração 5: signal=none; data=real_mt5; tick={'bid': 1.13911, 'ask': 1.13912, 'last': 0.0, 'tick_value': 1.0}; candle={'time': '2026.09.25 21:00', 'open': 1.13911, 'high': 1.1392, 'low': 1.13906, 'close': 1.13906, 'tick_volume': 147}

## Garantias

- O modo paper nunca chama `place_order`, `_write_command`, cancelamento, proteção ou redução.
- ZERO comandos `DWX_Commands_*` escritos.
- ZERO ordens reais e ZERO mutações no MT5.
- O gate fail-closed de `trading/safety.py` permanece intacto.
