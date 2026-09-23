"""Testes unitários do DrawdownMonitor."""
import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from risk.drawdown_monitor import DrawdownMonitor


def _make_snapshots(equities: list[float], market: str = "CRIPTO") -> list:
    """Cria lista de EquitySnapshot fake para testes."""
    import types

    now = datetime.datetime.now(datetime.timezone.utc)
    snapshots = []
    for i, eq in enumerate(equities):
        delta = datetime.timedelta(hours=i)
        s = types.SimpleNamespace(
            equity=Decimal(str(eq)),
            snapshot_at=now - datetime.timedelta(hours=len(equities) - i),
            market=market,
        )
        snapshots.append(s)
    return snapshots


def _make_config(**kw):
    import types

    defaults = dict(
        market="CRIPTO",
        is_active=True,
        mode="paper",
        drawdown_status="OK",
        drawdown_alert_pct=Decimal("10"),
        max_drawdown_pct=Decimal("15"),
        current_drawdown_1d=Decimal("0"),
        current_drawdown_30d=Decimal("0"),
        peak_capital=Decimal("10000"),
        paused_reason=None,
        paused_at=None,
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_calcula_drawdown_zero():
    """Capital constante → drawdown zero."""
    snapshots = _make_snapshots([10000, 10000, 10000])
    config = _make_config()

    db = AsyncMock()
    db.execute = AsyncMock()

    # Primeiro execute → snapshots 30d; segundo → config
    mock_result_snaps = MagicMock()
    mock_result_snaps.scalars.return_value.all.return_value = snapshots

    mock_result_cfg = MagicMock()
    mock_result_cfg.scalar_one_or_none.return_value = config

    db.execute.side_effect = [mock_result_snaps, mock_result_cfg]

    monitor = DrawdownMonitor(db)
    result = await monitor.calculate_drawdown("CRIPTO")

    assert result.dd_30d == Decimal("0")
    assert result.dd_1d == Decimal("0")


@pytest.mark.asyncio
async def test_calcula_drawdown_positivo():
    """Capital caiu de 10000 para 9000 → drawdown 10%."""
    snapshots = _make_snapshots([10000, 9500, 9000])
    config = _make_config(drawdown_status="OK")

    db = AsyncMock()
    mock_snaps = MagicMock()
    mock_snaps.scalars.return_value.all.return_value = snapshots
    mock_cfg = MagicMock()
    mock_cfg.scalar_one_or_none.return_value = config
    db.execute.side_effect = [mock_snaps, mock_cfg]

    monitor = DrawdownMonitor(db)
    result = await monitor.calculate_drawdown("CRIPTO")

    assert result.dd_30d == Decimal("10.00")
    assert result.peak_capital == Decimal("10000")
    assert result.current_equity == Decimal("9000")


@pytest.mark.asyncio
async def test_sem_snapshots_retorna_zero():
    db = AsyncMock()
    mock_snaps = MagicMock()
    mock_snaps.scalars.return_value.all.return_value = []
    db.execute.return_value = mock_snaps

    monitor = DrawdownMonitor(db)
    result = await monitor.calculate_drawdown("CRIPTO")

    assert result.dd_30d == Decimal("0")
    assert result.status == "OK"


@pytest.mark.asyncio
async def test_check_enforce_warning():
    """DD 30d = 12% → status WARNING."""
    snapshots = _make_snapshots([10000] + [8800] * 29)  # -12%
    config = _make_config(
        drawdown_alert_pct=Decimal("10"),
        max_drawdown_pct=Decimal("15"),
        drawdown_status="OK",
    )

    db = AsyncMock()
    db.commit = AsyncMock()

    # check_and_enforce faz 2 execute para config + calculate_drawdown faz 2 (snaps + cfg)
    mock_cfg_1 = MagicMock()
    mock_cfg_1.scalar_one_or_none.return_value = config

    mock_snaps = MagicMock()
    mock_snaps.scalars.return_value.all.return_value = snapshots

    mock_cfg_2 = MagicMock()
    mock_cfg_2.scalar_one_or_none.return_value = config

    db.execute.side_effect = [mock_cfg_1, mock_snaps, mock_cfg_2]

    monitor = DrawdownMonitor(db)
    # Silencia notificações
    monitor._notify_and_broadcast = AsyncMock()

    result = await monitor.check_and_enforce("CRIPTO")

    assert result.status == "WARNING"
    assert config.drawdown_status == "WARNING"
    monitor._notify_and_broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_enforce_paused():
    """DD 30d = 20% → status PAUSED, is_active=False."""
    snapshots = _make_snapshots([10000] + [8000] * 29)  # -20%
    config = _make_config(
        drawdown_alert_pct=Decimal("10"),
        max_drawdown_pct=Decimal("15"),
        drawdown_status="WARNING",
        is_active=True,
    )

    db = AsyncMock()
    db.commit = AsyncMock()

    mock_cfg_1 = MagicMock()
    mock_cfg_1.scalar_one_or_none.return_value = config

    mock_snaps = MagicMock()
    mock_snaps.scalars.return_value.all.return_value = snapshots

    mock_cfg_2 = MagicMock()
    mock_cfg_2.scalar_one_or_none.return_value = config

    db.execute.side_effect = [mock_cfg_1, mock_snaps, mock_cfg_2]

    monitor = DrawdownMonitor(db)
    monitor._notify_and_broadcast = AsyncMock()

    result = await monitor.check_and_enforce("CRIPTO")

    assert result.status == "PAUSED"
    assert config.is_active is False
    assert config.drawdown_status == "PAUSED"
