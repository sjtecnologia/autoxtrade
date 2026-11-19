from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from trading.mt5_snapshot import inspect_snapshot
from trading.dwx_readonly import DWXDataUnavailable

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'Autoxtrade_Market_Snapshot.json'
        self.now=int(datetime(2026,11,12,12,tzinfo=timezone.utc).timestamp())
        self.wall=datetime(2026,11,12,14)
        m=dict(schema='autoxtrade.mt5.metadata.v1',read_only=True,orders_sent=0,symbol='EURUSD.pr',
               observed_utc_epoch=self.now,last_server_wall_epoch=self.now+7200,estimated_server_wall_epoch=self.now+7200,tick_wall_epoch=self.now+7200,
               specifications=dict(point=.00001,tick_size=.00001,tick_value=1,contract_size=100000,volume_min=.01,volume_max=100,volume_step=.01),
               quote_sessions_raw=[dict(weekday_mql=d,index=0,from_seconds_raw=0,to_seconds_raw=86400) for d in range(1,6)])
        bars={(self.wall-timedelta(hours=i)).strftime('%Y.%m.%d %H:%M'):dict(open=1,high=2,low=1,close=2,tick_volume=10) for i in range(1,51)}
        self.data=dict(schema='autoxtrade.mt5.snapshot.v1',read_only=True,orders_sent=0,timeframe='H1',requested_bars=50,metadata=m,history={'EURUSD.pr_H1':bars})
    def tearDown(self):self.tmp.cleanup()
    def inspect(self,age=0):
        self.path.write_text(json.dumps(self.data));os.utime(self.path,(self.now-age,self.now-age))
        return inspect_snapshot(self.root,now=self.now)
    def test_complete_bundle(self):
        r=self.inspect();self.assertEqual(r['candles'],50);self.assertTrue(r['internal_and_tail_coverage_consistent'])
        self.assertEqual(r['trailing_unobserved_open_slots_at_snapshot'],0)
        for k in ('calendar_verified','historical_timezone_verified','execution_authorized','broker_timestamp_verified'):self.assertFalse(r[k])
    def test_stale_file(self):
        with self.assertRaises(DWXDataUnavailable):self.inspect(181)
    def test_old_payload_new_file(self):
        self.data['metadata']['observed_utc_epoch']-=181
        with self.assertRaises(ValueError):self.inspect()
    def test_future_payload(self):
        self.data['metadata']['observed_utc_epoch']+=60
        with self.assertRaises(ValueError):self.inspect()
    def test_wrong_symbol(self):
        self.data['metadata']['symbol']='GBPUSD.pr'
        with self.assertRaises(ValueError):self.inspect()
    def test_partial(self):
        self.data['history']['EURUSD.pr_H1'].popitem()
        with self.assertRaises(ValueError):self.inspect()
    def test_current_bar(self):
        bars=self.data['history']['EURUSD.pr_H1'];bars.popitem();bars[self.wall.strftime('%Y.%m.%d %H:%M')]=dict(open=1,high=2,low=1,close=2,tick_volume=10)
        with self.assertRaises(ValueError):self.inspect()
    def test_internal_hole(self):
        bars=self.data['history']['EURUSD.pr_H1'];bars.pop((self.wall-timedelta(hours=10)).strftime('%Y.%m.%d %H:%M'))
        bars[(self.wall-timedelta(hours=51)).strftime('%Y.%m.%d %H:%M')]=dict(open=1,high=2,low=1,close=2,tick_volume=10)
        self.assertFalse(self.inspect()['internal_and_tail_coverage_consistent'])
    def test_missing_file(self):
        with self.assertRaises(DWXDataUnavailable):inspect_snapshot(self.root,now=self.now)
    def test_no_output_mutation(self):
        self.inspect();before=self.path.read_bytes();inspect_snapshot(self.root,now=self.now);self.assertEqual(before,self.path.read_bytes())
    def test_truncated(self):
        self.path.write_text('{');os.utime(self.path,(self.now,self.now))
        with self.assertRaises(DWXDataUnavailable):inspect_snapshot(self.root,now=self.now)
    def test_no_order_marker(self):
        self.data['orders_sent']=1
        with self.assertRaises(ValueError):self.inspect()
    def test_wrong_timeframe(self):
        self.data['timeframe']='M1'
        with self.assertRaises(ValueError):self.inspect()
    def test_subdirectory(self):
        sub=self.root/'DWX';sub.mkdir();self.path=sub/self.path.name
        self.assertEqual(self.inspect()['status'],'inspected')

if __name__=='__main__':unittest.main()
