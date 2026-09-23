#!/usr/bin/env bash
# Sobe ambiente local no macOS com tentativa de mount SMB do feed MT5.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

SMB_HOST="${SMB_HOST:-192.168.0.52}"
SMB_SHARE="${SMB_SHARE:-MT5Files}"
SMB_USER="${SMB_USER:-}"

if [[ -f "$ENV_FILE" ]]; then
  MT5_MOUNT="$(grep -E '^MT5_FILES_DIR=' "$ENV_FILE" | head -n1 | cut -d'=' -f2-)"
else
  MT5_MOUNT="/Users/$USER/mt5files"
fi

MT5_MOUNT="${MT5_MOUNT:-/Users/$USER/mt5files}"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
MT5_EFFECTIVE="$MT5_MOUNT"

is_mounted() {
  mount | grep -q "on $MT5_MOUNT "
}

echo "[autoxtrade] Root: $ROOT_DIR"
echo "[autoxtrade] MT5 mountpoint: $MT5_MOUNT"

mkdir -p "$MT5_MOUNT"

# Se o share já estiver montado em outro ponto (ex.: /Volumes/MT5Files via Finder), reutiliza.
existing_mount="$(mount | sed -n "s#^//[^ ]*@$SMB_HOST/$SMB_SHARE on \([^ ]*\) .*#\1#p" | head -n1)"
if [[ -n "$existing_mount" ]]; then
  MT5_EFFECTIVE="$existing_mount"
  echo "[autoxtrade] Share já montado em $existing_mount (reutilizando)."
fi

if is_mounted || [[ -n "$existing_mount" ]]; then
  echo "[autoxtrade] SMB já montado em $MT5_MOUNT"
else
  if [[ -z "$SMB_USER" ]]; then
    echo "[autoxtrade] SMB_USER não definido."
    echo "[autoxtrade] Exemplo: SMB_USER=renato_win scripts/dev_start_mac.sh"
    echo "[autoxtrade] Pulando mount SMB (feed ficará fallback)."
  else
    echo "[autoxtrade] Montando //$SMB_USER@$SMB_HOST/$SMB_SHARE em $MT5_MOUNT ..."
    if mount_smbfs "//$SMB_USER@$SMB_HOST/$SMB_SHARE" "$MT5_MOUNT"; then
      echo "[autoxtrade] SMB montado com sucesso."
    else
      echo "[autoxtrade] Falha no mount SMB. Continuando com fallback."
    fi
  fi
fi

if [[ -f "$MT5_EFFECTIVE/DWX/DWX_Market_Data.txt" ]]; then
  echo "[autoxtrade] Feed MT5 detectado: $MT5_EFFECTIVE/DWX/DWX_Market_Data.txt"
else
  echo "[autoxtrade] Feed MT5 NÃO detectado. Gráfico ficará em fallback."
fi

echo "[autoxtrade] Reiniciando backend..."
pkill -f "uvicorn main:app" 2>/dev/null || true
(
  cd "$BACKEND_DIR"
  source .venv/bin/activate
  MT5_FILES_DIR="$MT5_EFFECTIVE" nohup uvicorn main:app --host 0.0.0.0 --port 8000 >/tmp/autoxtrade_backend.log 2>&1 &
)

sleep 2

# Garante que o EA publique DWX_Market_Data.txt para os símbolos usados no projeto.
(
  cd "$BACKEND_DIR"
  source .venv/bin/activate
  python3 - <<'PY' >/tmp/autoxtrade_subscribe.log 2>&1 || true
import asyncio
from config import settings
from trading.connectors.dwx import DWXConnector

SYMS = ["EURUSD.pr", "GBPUSD.pr", "USDJPY.pr", "XAUUSD.pr", "BTCUSD.lv", "ETHUSD.lv", "BCHUSD.lv"]

async def main():
    c = DWXConnector(mt5_files_dir=settings.mt5_files_dir)
    await c.connect()
    await c.subscribe_price_stream(SYMS)
    await asyncio.sleep(1)
    await c.disconnect()

asyncio.run(main())
PY
)

echo "[autoxtrade] Reiniciando frontend..."
lsof -ti :3001 | xargs kill -9 2>/dev/null || true
(
  cd "$FRONTEND_DIR"
  nohup npm run dev -- --port 3001 >/tmp/autoxtrade_frontend.log 2>&1 &
)

sleep 3

echo "[autoxtrade] Status endpoints:"
curl -s http://localhost:8000/api/v1/market/prices | head -c 200 || true
echo ""
echo "[autoxtrade] Frontend: http://localhost:3001/charts"
echo "[autoxtrade] Backend:  http://localhost:8000/docs"
echo "[autoxtrade] Logs backend:  tail -f /tmp/autoxtrade_backend.log"
echo "[autoxtrade] Logs frontend: tail -f /tmp/autoxtrade_frontend.log"
echo "[autoxtrade] Logs subscribe: tail -f /tmp/autoxtrade_subscribe.log"
