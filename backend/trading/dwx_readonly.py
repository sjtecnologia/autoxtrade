"""DWX_Server_MT5 1.0 readers. No commands, connectors, config or database imports.

File freshness is a local observation, never broker tick time or protection evidence.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import time
from zoneinfo import ZoneInfo


class DWXDataUnavailable(RuntimeError):
    pass


def directory(value):
    if not value or not str(value).strip():
        raise DWXDataUnavailable('Diretório MT5 não configurado')
    root = Path(value).expanduser()
    if (root / 'DWX_Market_Data.txt').is_file():
        return root
    if (root / 'DWX').is_dir():
        return root / 'DWX'
    raise DWXDataUnavailable('Pasta DWX indisponível')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DWXDataUnavailable('Chave JSON duplicada')
        result[key] = value
    return result


def snapshot(path, *, now, max_age, max_bytes=20_000_000):
    if not math.isfinite(max_age) or max_age <= 0:
        raise DWXDataUnavailable('Limite de idade inválido')
    try:
        before = path.stat()
        if before.st_size > max_bytes:
            raise DWXDataUnavailable('Arquivo DWX excede limite')
        with path.open('rb') as stream:
            raw = stream.read(max_bytes + 1)
        after = path.stat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size) or len(raw) != after.st_size:
            raise DWXDataUnavailable('Arquivo DWX mudou durante leitura')
        age = now - after.st_mtime
        if age < -2 or age > max_age:
            raise DWXDataUnavailable('Arquivo DWX antigo ou com horário futuro')
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique)
        if not isinstance(data, dict) or not data:
            raise DWXDataUnavailable('Payload DWX vazio/inválido')
        return data, age
    except (OSError, UnicodeError, ValueError) as exc:
        raise DWXDataUnavailable('Arquivo DWX indisponível ou JSON inválido') from exc


def number(value, *, positive=True):
    if isinstance(value, bool):
        raise DWXDataUnavailable('Número DWX inválido')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DWXDataUnavailable('Número DWX inválido') from None
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise DWXDataUnavailable('Número DWX fora dos limites')
    return result


@dataclass(frozen=True)
class ObservedQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    file_age_seconds: float
    broker_timestamp_verified: bool = False

    @property
    def midpoint(self):
        return (self.bid + self.ask) / 2


def read_quote(path, symbol, *, max_age=10, now=None):
    reference = time.time() if now is None else now
    data, age = snapshot(directory(path) / 'DWX_Market_Data.txt', now=reference, max_age=max_age)
    values = data.get(symbol)
    if not isinstance(values, dict):
        raise DWXDataUnavailable('Símbolo exato não publicado; não fazemos aliases')
    bid, ask = number(values.get('bid')), number(values.get('ask'))
    if ask < bid:
        raise DWXDataUnavailable('Ask inferior ao bid')
    return ObservedQuote(symbol, bid, ask, age)


def read_history(path, symbol, timeframe, *, server_timezone, seconds, max_age=7200, now=None):
    """Read existing EA export; reject gaps incl. weekends until a calendar exists."""
    if not server_timezone:
        raise DWXDataUnavailable('Timezone do histórico precisa ser explicitamente confirmado')
    try:
        zone = ZoneInfo(server_timezone)
    except (ValueError, KeyError):
        raise DWXDataUnavailable('Timezone inválido') from None
    reference = time.time() if now is None else now
    data, _ = snapshot(directory(path) / 'DWX_Historic_Data.txt', now=reference, max_age=max_age)
    bars = data.get(f'{symbol}_{timeframe}')
    if not isinstance(bars, dict) or not bars:
        raise DWXDataUnavailable('Histórico do símbolo/timeframe exatos indisponível')
    rows = []
    for stamp, values in bars.items():
        try:
            wall = datetime.strptime(stamp, '%Y.%m.%d %H:%M')
            a, b = wall.replace(tzinfo=zone, fold=0), wall.replace(tzinfo=zone, fold=1)
            if a.utcoffset() != b.utcoffset() or a.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != wall:
                raise DWXDataUnavailable('Timestamp ambíguo/inexistente por DST')
            utc = a.astimezone(timezone.utc)
            if utc.timestamp() % seconds:
                raise DWXDataUnavailable('Candle desalinhado')
            if not isinstance(values, dict):
                raise DWXDataUnavailable('Candle inválido')
            o, h, l, c = [number(values.get(k)) for k in ('open','high','low','close')]
            volume = number(values.get('tick_volume'), positive=False)
            if not (l <= min(o,c) <= max(o,c) <= h):
                raise DWXDataUnavailable('OHLC inconsistente')
            if utc.timestamp() + seconds <= reference:
                rows.append(dict(timestamp=utc, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(volume)))
        except (ValueError, TypeError):
            raise DWXDataUnavailable('Candle DWX inválido') from None
    rows.sort(key=lambda r: r['timestamp'])
    if not rows or reference - rows[-1]['timestamp'].timestamp() - seconds > max_age:
        raise DWXDataUnavailable('Sem histórico fechado recente')
    if any((b['timestamp']-a['timestamp']).total_seconds()!=seconds for a,b in zip(rows,rows[1:])):
        raise DWXDataUnavailable('Histórico com lacunas; calendário Forex ainda não validado')
    return rows


def main():
    import argparse
    from dataclasses import asdict
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True)
    parser.add_argument('--symbol',default='EURUSD.pr')
    args=parser.parse_args()
    try:
        report=dict(status='observed',quote=asdict(read_quote(args.directory,args.symbol)),orders_sent=0,execution_authorized=False)
    except DWXDataUnavailable as exc:
        report=dict(status='unavailable',error=str(exc),orders_sent=0,execution_authorized=False)
    print(json.dumps(report,default=str,ensure_ascii=False,indent=2))
    return 0 if report['status']=='observed' else 2

if __name__=='__main__':
    raise SystemExit(main())
