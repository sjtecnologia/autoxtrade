# Guia de Configuração — DWX Connect + MT5 (Equiti)

Este guia explica como instalar o **dwxconnect** (EA `dwx_server_mt5.mq5`) no
MetaTrader 5 da Equiti para que o **autoxtrade** possa operar via comunicação baseada
em arquivos JSON (sem ZeroMQ, sem DLLs externas).

Repositório oficial: <https://github.com/darwinex/dwxconnect>

---

## 1. Como funciona a comunicação

O EA e o Python se comunicam por **arquivos JSON** no diretório `MQL5/Files/DWX/`:

| Arquivo | Quem escreve | Conteúdo |
|---------|-------------|---------|
| `DWX_Commands_N.txt` (N = 0–49) | Python | Comandos: `<:id\|COMANDO\|conteúdo:>` |
| `DWX_Orders.txt` | EA | Ordens abertas + account_info |
| `DWX_Market_Data.txt` | EA | Preços bid/ask por símbolo |
| `DWX_Historic_Data.txt` | EA | Velas OHLCV históricas |

---

## 2. Pré-requisitos

| Item | Detalhe |
|------|---------|
| Plataforma | MetaTrader 5 (Windows) |
| Conta | Equiti (qualquer servidor) |
| Python | Roda em Mac/Linux/Windows com acesso ao diretório acima |

---

## 3. Instalar o Expert Advisor

### 3.1 Clone o repositório no Windows

```powershell
git clone https://github.com/darwinex/dwxconnect.git
```

Ou baixe em: <https://github.com/darwinex/dwxconnect/archive/refs/heads/master.zip>

### 3.2 Copiar o EA para o MT5

Copie o arquivo:
```
dwxconnect/mql/dwx_server_mt5.mq5
```
para:
```
C:\Users\<SEU_USUARIO>\AppData\Roaming\MetaQuotes\Terminal\<ID>\MQL5\Experts\
```

> Dica: no MT5, vá em **Arquivo → Abrir pasta de dados** para localizar o caminho `<ID>`.

### 3.3 Compilar no MetaEditor

1. Abra o **MetaEditor** — pressione **F4** no MT5, ou clique em **Tools → MetaQuotes Language Editor**.
2. No painel esquerdo (**Navigator**), expanda **Expert Advisors** e clique duplo em `dwx_server_mt5.mq5`.
3. Pressione **F7** (ou clique em **Compile**) para compilar.
4. Confirme **"0 errors, 0 warnings"** na aba **Errors** na parte inferior.

---

## 4. Configurar o MT5

No MT5 → **Tools → Options → Expert Advisors** (PT: Ferramentas → Opções → Assessores Especialistas), marque:
- ✅ **Allow automated trading** (PT: Permitir operações de negociação automáticas)

> **Não** é necessário instalar ZeroMQ nem nenhuma DLL externa.

---

## 5. Anexar o EA ao gráfico

1. Abra um gráfico (ex: **EURUSD H1**).
2. No painel **Navigator** (Ctrl+N para abrir), expanda **Expert Advisors** e arraste `dwx_server_mt5` para o gráfico.
3. Uma janela de configuração abre **automaticamente** ao soltar o EA no gráfico. Ela tem duas abas:
   - Aba **Inputs** (ou **Parameters**) — configure os parâmetros abaixo:

| Parâmetro | Valor recomendado | Descrição |
|-----------|------------------|-----------|
| `MaximumOrders` | `10` | Limite de ordens simultâneas |
| `MaximumLotSize` | `0.10` | Tamanho máximo do lote |
| `MILLISECOND_TIMER` | `25` | Intervalo de polling do EA (ms) |
| `numLastMessages` | `1000` | Mensagens de log retornadas |

   - Aba **Common** — marque **"Allow live trading"**.

   > Se a janela não abriu, clique com o botão direito no ícone do EA no gráfico → **Properties**.

4. Clique **OK** — confirme o ícone **sorridente** (😊) no canto superior direito do gráfico.

---

## 6. Configurar o autoxtrade

### 6.1 Acesso ao diretório MQL5/Files

O Python precisa de acesso de leitura/escrita ao diretório `MQL5/Files` do MT5.

**Se Python roda no mesmo Windows:** use o caminho local.

**Se Python roda em Mac/Linux:** compartilhe via SMB:

```powershell
# No Windows (PowerShell como administrador)
$filesPath = "$env:APPDATA\MetaQuotes\Terminal\<ID>\MQL5\Files"
New-SmbShare -Name "MT5Files" -Path $filesPath -FullAccess "Everyone"
```

```bash
# No Mac — criar pasta e montar o share (sem credenciais, se compartilhamento for guest/Kerberos)
mkdir -p ~/mt5files
mount_smbfs //IP-WINDOWS/MT5Files ~/mt5files
```

### 6.2 Configurar `.env`

No arquivo `backend/.env`, adicione:

```dotenv
# DWX Connect — caminho para MQL5/Files do MT5
# MT5 na mesma máquina (Windows):
# MT5_FILES_DIR=C:\Users\<USUARIO>\AppData\Roaming\MetaQuotes\Terminal\<ID>\MQL5\Files
# MT5 em outra máquina (Mac com SMB mount):
MT5_FILES_DIR=~/mt5files
# Valor real utilizado (já configurado):
# MT5_FILES_DIR=/Users/renato/mt5files
```

---

## 7. Inserir BotConfig FOREX no banco

```bash
cd backend
source .venv/bin/activate
python scripts/seed_db.py
```

Saída esperada:
```
[seed] BotConfig inserido: market=FOREX
[seed] Concluído.
```

---

## 8. Testar a conexão

Com o MT5 + EA ativos e o diretório montado:

```bash
cd backend
source .venv/bin/activate
python - <<'EOF'
import asyncio, os
from trading.connectors.dwx import DWXConnector

async def test():
    mt5_dir = os.environ.get("MT5_FILES_DIR", "")
    c = DWXConnector(mt5_files_dir=mt5_dir)
    await c.connect()
    await asyncio.sleep(1)
    bal = await c.get_balance()
    print("Saldo:", bal)
    price = await c.get_current_price("EURUSD")
    print("EURUSD:", price)
    await c.disconnect()

asyncio.run(test())
EOF
```

---

## 9. Arquitetura da comunicação

```
┌─────────────────────────────────────────────┐
│  autoxtrade (Mac/Linux/Windows)             │
│  DWXConnector (Python)                      │
│                                             │
│  Escreve: DWX_Commands_N.txt               │
│  Lê:      DWX_Orders.txt                   │
│           DWX_Market_Data.txt               │
│           DWX_Historic_Data.txt             │
└──────────────────┬──────────────────────────┘
                   │ Filesystem (local ou SMB)
┌──────────────────▼──────────────────────────┐
│  MetaTrader 5 (Windows)                     │
│  dwx_server_mt5.ex5 (EA)                    │
│  MQL5/Files/DWX/                            │
│  Conta Equiti                               │
└─────────────────────────────────────────────┘
```

---

## Observações importantes

- O EA precisa estar **ativo no gráfico** (ícone sorridente) para responder comandos.
- Reiniciar o MT5 desanexa o EA — reanexar manualmente ou configurar inicialização automática.
- Em modo **paper** (`mode=paper` no BotConfig), as ordens são simuladas — o EA não é acionado.
- Para operar ao vivo: `PATCH /api/v1/config/FOREX` com `{"mode": "live"}`.
- Símbolo `XAUUSD` = Ouro — verifique se a conta Equiti tem este instrumento habilitado.
- O Python faz polling a cada 5 ms por padrão (`sleep_delay=0.005`). Ajuste se necessário.
