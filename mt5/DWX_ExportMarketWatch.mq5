//+------------------------------------------------------------------+
//|  DWX_ExportMarketWatch.mq5                                       |
//|  Exporta os simbolos do MT5 para MQL5/Files/DWX/                 |
//|  MarketWatchSymbols.txt, consumido pelo autoxtrade em            |
//|  POST /api/v1/config/import-symbols/from-file                    |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

input bool InpAllBrokerSymbols = false;  // true = todos do broker; false = apenas Market Watch
input bool InpOnlyTradable     = true;   // ignora simbolos com negociacao desabilitada

int OnStart()
  {
   string dir = "DWX";
   if(!FolderCreate(dir, FILE_COMMON == 0 ? 0 : 0))
      Print("Pasta DWX ja existe ou nao pode ser criada (seguindo).");

   int handle = FileOpen(dir + "\\MarketWatchSymbols.txt", FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle == INVALID_HANDLE)
     {
      Print("ERRO: nao foi possivel abrir MarketWatchSymbols.txt. Codigo=", GetLastError());
      return(1);
     }

   int total   = SymbolsTotal(!InpAllBrokerSymbols);
   int written = 0;

   for(int i = 0; i < total; i++)
     {
      string symbol = SymbolName(i, !InpAllBrokerSymbols);
      if(symbol == "")
         continue;

      if(InpOnlyTradable)
        {
         long mode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
         if(mode == SYMBOL_TRADE_MODE_DISABLED)
            continue;
        }

      FileWrite(handle, symbol);
      written++;
     }

   FileClose(handle);
   PrintFormat("MarketWatchSymbols.txt gerado com %d simbolos (fonte: %s).",
               written, InpAllBrokerSymbols ? "todos do broker" : "Market Watch");
   return(0);
  }
//+------------------------------------------------------------------+
