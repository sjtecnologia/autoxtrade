# Integração MT5/DWX - modo somente leitura

- Caminho lido: `/Volumes/MT5Files/DWX`
- Data da leitura: `2026-09-25T14:02:54.234663+00:00`
- Arquivos `DWX_*`: DWX_Market_Data.txt, DWX_Orders.txt
- Padrão derivado: `DWX_<tipo>.txt`
- Fonte de ordens: `/Volumes/MT5Files/DWX/DWX_Orders.txt`
- Fonte de market data: `/Volumes/MT5Files/DWX/DWX_Market_Data.txt`
- Símbolos encontrados (hint): AMD, AUDUSD.pr, Apple, BBDC4, BCHUSD.lv, BTCUSD.lv, ETHUSD.lv, EURGBP.pr, EURJPY.pr, EURUSD.pr, Facebook, GBPUSD.pr, ITUB4, LTCUSD.lv, Microsoft, NVIDIA, NZDUSD.pr, Netflix, PETR4, Tesla, USDCAD.pr, USDJPY.pr, VALE3, XAUUSD.pr
- Símbolos inválidos: nenhum
- Idade média dos arquivos `DWX_*`: 5084.7s
- Market data ausente: `True`
- Dado não fresco: `True`
- `small_gaps_filled`: 0
- Gaps grandes: 0
- Ausências: DWX_Market_Data.txt (vazio/sem payload), DWX_Commands_*, DWX_Positions_*, DWX_Open_Orders_*
- Divergências/órfãos: 0

## Achados

- ACHADO: servidor usa DWX_Orders.txt/DWX_Market_Data.txt sem sufixo; o conector/protocolo espera variantes com slot (_N) em alguns canais; recomendação: parametrizar nomes ou alinhar o conector ao EA.
- MarketWatchSymbols.txt usado somente como hint de símbolos suportados.

## Dry-run

- Iterações executadas: 1
- OrderMonitor/reconciliação: somente consulta e sinalização.
- ZERO comandos DWX escritos: confirmado por guarda e snapshot.
- ZERO ordens enviadas ou mutações: confirmado por guarda de métodos.
