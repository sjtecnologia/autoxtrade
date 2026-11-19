"""Offline weekly-session audit in broker wall time, not an execution authority.

Never infers a historical UTC zone or fills missing candles. Current exported
sessions classify gaps provisionally; they do not prove past holiday/schedule rules.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from trading.mt5_metadata import inspect_metadata_payload

STEPS={'M1':60,'M3':180,'M5':300,'M15':900,'M30':1800,'H1':3600,'H2':7200,'H4':14400,'H6':21600,'H8':28800,'H12':43200,'D1':86400}


def sessions_from_rows(rows):
    if not rows:
        raise ValueError('Sem sessões: não inferir calendário 24/7')
    windows=[]
    for row in rows:
        day=row['weekday_mql'];start=row['from_seconds_raw'];end=row['to_seconds_raw']
        if not 0<=start<86400 or not 0<=end<=172800 or start==end:
            raise ValueError('Sessão com limites ambíguos/não suportados')
        if end<start:end+=86400
        if end-start>86400:
            raise ValueError('Sessão excede um dia')
        windows.append(((day+6)%7,start,end))  # Python: Monday=0
    # Overlap is ambiguous, including Sunday crossing into Monday.
    expanded=[]
    for day,start,end in windows:
        left=day*86400+start;right=day*86400+end
        expanded.append((left,right))
        if right>7*86400:expanded.append((left-7*86400,right-7*86400))
    expanded.sort()
    if any(b[0]<a[1] for a,b in zip(expanded,expanded[1:])):
        raise ValueError('Sessões sobrepostas')
    return tuple(windows)


def intersects_session(left,right,windows):
    # Also inspect previous day for sessions crossing midnight.
    day=left.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=1)
    while day<right:
        for weekday,start,end in windows:
            if day.weekday()==weekday:
                a=day+timedelta(seconds=start);b=day+timedelta(seconds=end)
                if max(a,left)<min(b,right):return True
        day+=timedelta(days=1)
    return False


def load_unique(path):
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError('Chave JSON duplicada')
            result[k]=v
        return result
    with Path(path).open('rb') as stream:raw=stream.read(20_000_001)
    if len(raw)>20_000_000:raise ValueError('Histórico excede limite')
    return json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique)


def audit_history(history_path,metadata_path,*,symbol='EURUSD.pr',timeframe='H1'):
    return audit_payload(load_unique(history_path),load_unique(metadata_path),symbol=symbol,timeframe=timeframe)


def audit_payload(data,raw,*,symbol='EURUSD.pr',timeframe='H1'):
    from trading.dwx_readonly import number
    metadata=inspect_metadata_payload(raw)
    if symbol!=metadata['symbol']:raise ValueError('Símbolo difere dos metadados')
    if timeframe not in STEPS:raise ValueError('Timeframe não suportado neste auditor')
    windows=sessions_from_rows(metadata['quote_sessions_raw'])
    bars=data.get(symbol+'_'+timeframe) if isinstance(data,dict) else None
    if not isinstance(bars,dict) or not bars:raise ValueError('Histórico exato indisponível')
    # MQL datetime encodes broker wall-clock fields; UTC here decodes fields only.
    # This is NOT conversion of historical candles to UTC.
    observed_wall=datetime.fromtimestamp(raw['last_server_wall_epoch'],timezone.utc).replace(tzinfo=None)
    step=STEPS[timeframe];delta=timedelta(seconds=step)
    stamps=[];excluded=[]
    for text,values in bars.items():
        stamp=datetime.strptime(text,'%Y.%m.%d %H:%M')
        if (stamp.hour*3600+stamp.minute*60)%step:raise ValueError('Candle desalinhado')
        if not isinstance(values,dict):raise ValueError('Candle inválido')
        o,h,l,c=[number(values.get(k)) for k in ('open','high','low','close')]
        number(values.get('tick_volume'),positive=False)
        if not l<=min(o,c)<=max(o,c)<=h:raise ValueError('OHLC inválido')
        if stamp+delta>observed_wall:excluded.append(stamp)
        else:stamps.append(stamp)
    stamps.sort()
    if not stamps:raise ValueError('Sem candles fechados no instante da coleta')
    if (stamps[-1]-stamps[0]).total_seconds()/step>200000:raise ValueError('Intervalo excede limite')
    gaps=[];unexplained=0;closed_slots=0
    for a,b in zip(stamps,stamps[1:]):
        cursor=a+delta;missing=0;open_slots=0
        while cursor<b:
            missing+=1
            if intersects_session(cursor,cursor+delta,windows):open_slots+=1
            cursor+=delta
        if missing:
            unexplained+=open_slots;closed_slots+=missing-open_slots
            gaps.append(dict(previous_candle=str(a),next_candle=str(b),missing_slots=missing,
                             slots_during_published_sessions=open_slots,
                             classification='unexplained_missing_data' if open_slots else 'consistent_with_current_weekly_closure'))
    outside=[str(s) for s in stamps if not intersects_session(s,s+delta,windows)]
    trailing=0
    cursor=stamps[-1]+delta
    if (observed_wall-cursor).total_seconds()/step>200000:
        raise ValueError('Cobertura final excede limite')
    while cursor+delta<=observed_wall:
        if intersects_session(cursor,cursor+delta,windows):trailing+=1
        cursor+=delta
    return dict(status='audited',symbol=symbol,timeframe=timeframe,time_basis='broker_wall_time_no_utc_conversion',
                observed_server_wall_time=str(observed_wall),candles_total=len(bars),closed_at_snapshot=len(stamps),
                excluded_not_closed_at_snapshot=len(excluded),first_candle=str(stamps[0]),last_closed_candle=str(stamps[-1]),
                gaps=gaps,unexplained_missing_slots=unexplained,slots_consistent_with_weekly_closure=closed_slots,
                candles_outside_current_sessions=outside,
                consistent_with_current_weekly_schedule=unexplained==0 and not outside,
                trailing_unobserved_open_slots_at_snapshot=trailing,
                internal_and_tail_coverage_consistent=unexplained==0 and not outside and trailing==0,
                boundary_coverage_verified=False,calendar_verified=False,historical_timezone_verified=False,
                holidays_verified=False,orders_sent=0,execution_authorized=False,
                limitations=['Sessões atuais não provam vigência histórica ou feriados.',
                             'Cobertura anterior ao primeiro candle não verificada; cobertura final comparada apenas ao instante da coleta.',
                             'Nenhum candle é criado, convertido para UTC ou liberado para execução.'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history',required=True);p.add_argument('--metadata',required=True)
    p.add_argument('--symbol',default='EURUSD.pr');p.add_argument('--timeframe',default='H1')
    a=p.parse_args()
    try:r=audit_history(a.history,a.metadata,symbol=a.symbol,timeframe=a.timeframe)
    except (OSError,ValueError,TypeError,KeyError,RuntimeError) as exc:r=dict(status='invalid',error=str(exc),execution_authorized=False,orders_sent=0)
    print(json.dumps(r,indent=2,ensure_ascii=False))
    return 0 if r['status']=='audited' and r['internal_and_tail_coverage_consistent'] else 2

if __name__=='__main__':raise SystemExit(main())
