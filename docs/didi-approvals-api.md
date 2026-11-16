# Aprovação de Entradas DiDi

## Variáveis

- `BASE_URL`: `http://localhost:8000`
- `TOKEN`: mesmo valor de `API_SECRET_TOKEN`

## 1) Listar entradas pendentes

```bash
curl -sS -X GET "$BASE_URL/api/v1/approvals/entries?status=pending" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json"
```

## 2) Aprovar entrada

```bash
curl -sS -X POST "$BASE_URL/api/v1/approvals/entries/{approval_id}/approve" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json"
```

Resposta esperada:

```json
{
  "id": "2a89c1d6-ff0c-4f3e-b7e2-9bf2d0912f04",
  "status": "approved"
}
```

## 3) Rejeitar entrada

```bash
curl -sS -X POST "$BASE_URL/api/v1/approvals/entries/{approval_id}/reject" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json"
```

Resposta esperada:

```json
{
  "id": "2a89c1d6-ff0c-4f3e-b7e2-9bf2d0912f04",
  "status": "rejected"
}
```

## Exemplo de payload de uma entrada pendente

```json
{
  "id": "2a89c1d6-ff0c-4f3e-b7e2-9bf2d0912f04",
  "status": "pending",
  "market": "CRIPTO",
  "symbol": "BTC/USDT",
  "side": "buy",
  "mode": "paper",
  "entry_price": "65200.10",
  "stop_loss": "64680.40",
  "take_profit": "66239.50",
  "quantity": "0.00450000",
  "criteria": {
    "didi_signal": true,
    "dmi_trend": true,
    "adx_accelerating": true,
    "bollinger_open": true
  },
  "details": {
    "didi_reason": "agulhada_compra",
    "adx": "31.45",
    "plus_di": "24.90",
    "minus_di": "13.30",
    "bb_width": "0.0134",
    "bb_width_ma": "0.0101"
  },
  "created_at": "2026-08-07T13:10:12.000000+00:00",
  "expires_at": "2026-08-07T13:40:12.000000+00:00"
}
```
