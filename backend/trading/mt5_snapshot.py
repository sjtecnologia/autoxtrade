"""Inspect a coherent automatically collected MT5 snapshot. No execution capability."""
import argparse
import json
from pathlib import Path
import time

from trading.dwx_readonly import snapshot, DWXDataUnavailable
from trading.mt5_calendar import audit_payload
from trading.mt5_metadata import inspect_metadata_payload


def inspect_snapshot(directory, *, now=None, max_age=180):
    root=Path(directory).expanduser()
    if not (root/'Autoxtrade_Market_Snapshot.json').is_file() and (root/'DWX').is_dir():root=root/'DWX'
    reference=time.time() if now is None else now
    payload,file_age=snapshot(root/'Autoxtrade_Market_Snapshot.json',now=reference,max_age=max_age)
    if payload.get('schema')!='autoxtrade.mt5.snapshot.v1' or payload.get('read_only') is not True or type(payload.get('orders_sent'))!=int or payload['orders_sent']!=0:
        raise ValueError('Contrato de snapshot inválido')
    if payload.get('timeframe')!='H1':raise ValueError('Snapshot deve ser H1')
    count=payload.get('requested_bars')
    if type(count)!=int or not 50<=count<=5000:raise ValueError('Quantidade solicitada inválida')
    metadata=payload.get('metadata');history=payload.get('history')
    report=inspect_metadata_payload(metadata,now=reference)
    if report['symbol']!='EURUSD.pr':raise ValueError('Snapshot não pertence a EURUSD.pr')
    if not isinstance(history,dict) or set(history)!= {'EURUSD.pr_H1'} or not isinstance(history['EURUSD.pr_H1'],dict):
        raise ValueError('Histórico exato inválido')
    if len(history['EURUSD.pr_H1'])!=count:raise ValueError('Coleta parcial: quantidade difere do solicitado')
    age=report['observation_age_seconds']
    if age>max_age or age < -2:raise ValueError('Snapshot com relógio antigo ou futuro')
    if abs(age-file_age)>30:raise ValueError('Relógio UTC da coleta diverge da publicação local')
    audit=audit_payload(history,metadata)
    if audit['excluded_not_closed_at_snapshot']:
        raise ValueError('Snapshot contém candle ainda aberto na coleta')
    return dict(status='inspected',symbol=report['symbol'],file_age_seconds=file_age,
                observation_age_seconds=age,observed_utc_epoch=report['observed_utc_epoch'],
                candles=count,last_closed_candle=audit['last_closed_candle'],
                trailing_unobserved_open_slots_at_snapshot=audit['trailing_unobserved_open_slots_at_snapshot'],
                unexplained_missing_slots=audit['unexplained_missing_slots'],
                internal_and_tail_coverage_consistent=audit['internal_and_tail_coverage_consistent'],
                candles_outside_current_sessions=len(audit['candles_outside_current_sessions']),
                excluded_not_closed_at_snapshot=audit['excluded_not_closed_at_snapshot'],
                current_offset_candidate_seconds=report['current_offset_candidate_seconds'],
                metadata_warnings=report['warnings'],specifications_complete=report['specifications_complete'],
                calendar_verified=False,historical_timezone_verified=False,
                broker_timestamp_verified=False,account_identity_verified=False,
                orders_sent=0,execution_authorized=False)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',required=True)
    a=p.parse_args()
    try:r=inspect_snapshot(a.directory)
    except (OSError,ValueError,TypeError,KeyError,RuntimeError) as exc:r=dict(status='unavailable',error=str(exc),orders_sent=0,execution_authorized=False)
    print(json.dumps(r,indent=2,ensure_ascii=False))
    return 0 if r['status']=='inspected' and r['internal_and_tail_coverage_consistent'] and not r['metadata_warnings'] and r['specifications_complete'] else 2

if __name__=='__main__':raise SystemExit(main())
