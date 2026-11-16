import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from trading.dwx_readonly import read_quote, read_history, DWXDataUnavailable
from trading.market_feed import latest_mark_price, load_ohlcv, MarketDataUnavailable


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.dwx=self.root/'DWX';self.dwx.mkdir()
        self.now=datetime(2026,11,16,14,tzinfo=timezone.utc).timestamp()
        self.write('DWX_Market_Data.txt',{'EURUSD.pr':{'bid':1.1,'ask':1.2}})
        self.settings=SimpleNamespace(mt5_files_dir=str(self.root),mt5_readonly_feed_enabled=True,
            mt5_quote_max_age_sec=10,mt5_history_timezone='UTC',didi_max_quote_age_sec=7200)
    def tearDown(self):self.temp.cleanup()
    def write(self,name,value,age=0):
        p=self.dwx/name;p.write_text(json.dumps(value));os.utime(p,(self.now-age,self.now-age));return p
    def test_quote_root_and_subfolder(self):
        for path in (self.root,self.dwx):
            q=read_quote(path,'EURUSD.pr',now=self.now)
            self.assertEqual(q.midpoint,Decimal('1.15'));self.assertFalse(q.broker_timestamp_verified)
    def test_exact_symbol(self):
        with self.assertRaises(DWXDataUnavailable):read_quote(self.root,'EURUSD',now=self.now)
    def test_old_and_future(self):
        for age in (11,-10):
            self.write('DWX_Market_Data.txt',{'EURUSD.pr':{'bid':1,'ask':2}},age)
            with self.assertRaises(DWXDataUnavailable):read_quote(self.root,'EURUSD.pr',now=self.now)
    def test_invalid_prices(self):
        for bid,ask in [(0,1),(2,1),(float('nan'),2),(True,2),(-1,2)]:
            self.write('DWX_Market_Data.txt',{'EURUSD.pr':{'bid':bid,'ask':ask}})
            with self.assertRaises(DWXDataUnavailable):read_quote(self.root,'EURUSD.pr',now=self.now)
    def test_incomplete_empty_duplicate(self):
        for raw in ('{','{}','{"EURUSD.pr":{},"EURUSD.pr":{}}'):
            p=self.dwx/'DWX_Market_Data.txt';p.write_text(raw);os.utime(p,(self.now,self.now))
            with self.assertRaises(DWXDataUnavailable):read_quote(self.root,'EURUSD.pr',now=self.now)
    def test_missing(self):
        (self.dwx/'DWX_Market_Data.txt').unlink()
        with self.assertRaises(DWXDataUnavailable):read_quote(self.root,'EURUSD.pr',now=self.now)
    def test_mark_prioritizes_selected_feed(self):
        redis=SimpleNamespace(get=AsyncMock(return_value='999'))
        before={p.name:p.read_bytes() for p in self.dwx.iterdir()}
        result=asyncio.run(latest_mark_price(redis,'EURUSD.pr','FOREX',self.settings,now=datetime.fromtimestamp(self.now,timezone.utc)))
        self.assertEqual(result,Decimal('1.15'));redis.get.assert_not_called()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.dwx.iterdir()})
    def test_unavailable_no_fallback(self):
        self.write('DWX_Market_Data.txt',{},0)
        redis=SimpleNamespace(get=AsyncMock(return_value='999'))
        with self.assertRaises(MarketDataUnavailable):asyncio.run(latest_mark_price(redis,'EURUSD.pr','FOREX',self.settings,now=datetime.fromtimestamp(self.now,timezone.utc)))
        redis.get.assert_not_called()
    def bars(self):
        return {'EURUSD.pr_H1':{f'2026.11.16 {h:02d}:00':dict(open=1.1,high=1.3,low=1,close=1.2,tick_volume=10) for h in (11,12,13,14)}}
    def history(self,zone='UTC'):
        return read_history(self.root,'EURUSD.pr','H1',server_timezone=zone,seconds=3600,now=self.now)
    def test_history_excludes_open_candle(self):
        self.write('DWX_Historic_Data.txt',self.bars());rows=self.history()
        self.assertEqual(len(rows),3);self.assertEqual(rows[-1]['timestamp'].hour,13)
    def test_history_missing_zone(self):
        self.write('DWX_Historic_Data.txt',self.bars())
        with self.assertRaises(DWXDataUnavailable):self.history('')
    def test_history_gap(self):
        bars=self.bars();del bars['EURUSD.pr_H1']['2026.11.16 12:00'];self.write('DWX_Historic_Data.txt',bars)
        with self.assertRaises(DWXDataUnavailable):self.history()
    def test_history_ohlc_invalid(self):
        bars=self.bars();bars['EURUSD.pr_H1']['2026.11.16 12:00']['high']=.5;self.write('DWX_Historic_Data.txt',bars)
        with self.assertRaises(DWXDataUnavailable):self.history()
    def test_history_missing_not_synthesized(self):
        with self.assertRaises(MarketDataUnavailable):load_ohlcv('EURUSD.pr','FOREX',self.settings,timeframe='1h')
        self.assertFalse((self.dwx/'DWX_Historic_Data.txt').exists())
    def test_dst_ambiguous(self):
        self.write('DWX_Historic_Data.txt',{'EURUSD.pr_H1':{'2026.10.25 03:00':dict(open=1,high=2,low=1,close=2,tick_volume=1)}})
        with self.assertRaises(DWXDataUnavailable):self.history('Europe/Athens')
    def test_diagnostics_no_secrets_or_private_payload(self):
        from trading.mt5_diagnostics import read_only_report
        self.write('DWX_Orders.txt',{'private':'DO-NOT-EXPORT'})
        (self.dwx/'MarketWatchSymbols.txt').write_text('EURUSD.pr\nGBPUSD.pr')
        before={p.name:p.read_bytes() for p in self.dwx.iterdir()}
        result=read_only_report(self.root)
        self.assertNotIn('DO-NOT-EXPORT',str(result))
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['symbol_hints'],['EURUSD.pr','GBPUSD.pr'])
        self.assertNotIn('DWX_Commands_*',result['missing_sources'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.dwx.iterdir()})
    def test_no_empty_directory_fallback(self):
        with self.assertRaises(DWXDataUnavailable):read_quote('', 'EURUSD.pr',now=self.now)
    def test_other_markets_unchanged(self):
        redis=SimpleNamespace(get=AsyncMock(return_value='99'))
        self.assertEqual(asyncio.run(latest_mark_price(redis,'BTC/USDT','CRIPTO',self.settings)),Decimal('99'))

if __name__=='__main__':unittest.main()
