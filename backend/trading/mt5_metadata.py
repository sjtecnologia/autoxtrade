"""Validate observational MT5 script exports; never authorizes execution or DST rules."""
import argparse
import json
import math
from pathlib import Path
import time

SCHEMA='autoxtrade.mt5.metadata.v1'


def inspect_metadata(path, *, now=None):
    path=Path(path)
    with path.open('rb') as stream:
        raw=stream.read(1_000_001)
    if len(raw)>1_000_000:
        raise ValueError('Exportação excede limite')
    def unique(pairs):
        values={}
        for key,value in pairs:
            if key in values:raise ValueError('Chaves duplicadas')
            values[key]=value
        return values
    data=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique)
    return inspect_metadata_payload(data, now=now)


def inspect_metadata_payload(data, *, now=None):
    if not isinstance(data,dict) or data.get('schema')!=SCHEMA or data.get('read_only') is not True or data.get('orders_sent')!=0:
        raise ValueError('Contrato de metadados inválido')
    def numeric(value):
        return type(value) in (int,float) and math.isfinite(value)
    clocks=[data.get(k) for k in ('observed_utc_epoch','last_server_wall_epoch','estimated_server_wall_epoch','tick_wall_epoch')]
    if not all(numeric(v) and v>0 for v in clocks):raise ValueError('Relógios ausentes/inválidos')
    utc,last,estimated,tick=clocks
    age=(time.time() if now is None else now)-utc
    if age < -5:raise ValueError('Relógio UTC do terminal está no futuro; verificar relógios')
    offset=round((estimated-utc)/900)*900
    warnings=[]
    if abs(offset)>14*3600 or abs(estimated-utc-offset)>5:
        offset=None;warnings.append('Deslocamento atual inconclusivo')
    if abs(estimated-last)>60 or abs(estimated-tick)>60:
        warnings.append('Último tick/horário de cotação distante do relógio estimado')
    if age>300:warnings.append('Coleta tem mais de cinco minutos; usar somente para inspeção histórica')
    symbol=data.get('symbol')
    if not isinstance(symbol,str) or not symbol or len(symbol)>100:raise ValueError('Símbolo inválido')
    specs=data.get('specifications')
    if not isinstance(specs,dict):raise ValueError('Especificações ausentes')
    required=('point','tick_size','tick_value','contract_size','volume_min','volume_max','volume_step')
    specs_ok=all(numeric(specs.get(k)) and specs[k]>0 for k in required)
    if specs_ok:specs_ok=specs['volume_max']>=specs['volume_min']
    if not specs_ok:warnings.append('Especificações incompletas/inconsistentes')
    sessions=data.get('quote_sessions_raw')
    if not isinstance(sessions,list) or len(sessions)>224:raise ValueError('Sessões inválidas')
    seen=set()
    for item in sessions:
        if not isinstance(item,dict):raise ValueError('Sessão inválida')
        day,index=item.get('weekday_mql'),item.get('index')
        if type(day)!=int or not 0<=day<=6 or type(index)!=int or not 0<=index<32 or (day,index) in seen:
            raise ValueError('Dia/índice de sessão inválido')
        seen.add((day,index))
        if not all(type(item.get(k))==int and item[k]>=0 for k in ('from_seconds_raw','to_seconds_raw')):
            raise ValueError('Horários de sessão inválidos')
    if not sessions:warnings.append('Sem sessões publicadas; não presumir calendário 24/7')
    return dict(status='inspected',symbol=symbol,observed_utc_epoch=utc,
                observation_age_seconds=age,current_offset_candidate_seconds=offset,
                specifications=specs,specifications_complete=specs_ok,quote_sessions_raw=sessions,
                warnings=warnings,clock_source='MT5 TimeGMT / TimeTradeServer; dependem do relógio local',
                historical_timezone_verified=False,calendar_verified=False,account_identity_verified=False,
                orders_sent=0,execution_authorized=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--file',required=True)
    args=parser.parse_args()
    try:report=inspect_metadata(args.file)
    except (OSError,ValueError,TypeError) as exc:report=dict(status='invalid',error=str(exc),orders_sent=0,execution_authorized=False)
    print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0 if report['status']=='inspected' else 2

if __name__=='__main__':raise SystemExit(main())
