import json
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import unittest
from trading.mt5_calendar import audit_history,sessions_from_rows,intersects_session

class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.history=self.root/'history.json';self.metadata=self.root/'metadata.json'
        utc=int(datetime(2026,11,16,17,tzinfo=timezone.utc).timestamp())
        self.m=dict(schema='autoxtrade.mt5.metadata.v1',read_only=True,orders_sent=0,symbol='EURUSD.pr',observed_utc_epoch=utc,
          last_server_wall_epoch=utc+7200,estimated_server_wall_epoch=utc+7200,tick_wall_epoch=utc+7200,
          specifications={},quote_sessions_raw=[dict(weekday_mql=d,index=0,from_seconds_raw=0,to_seconds_raw=86400) for d in range(1,6)])
    def tearDown(self):self.tmp.cleanup()
    def run_audit(self,stamps):
        bar=dict(open=1,high=2,low=1,close=2,tick_volume=1)
        self.history.write_text(json.dumps({'EURUSD.pr_H1':{s:bar for s in stamps}}));self.metadata.write_text(json.dumps(self.m))
        return audit_history(self.history,self.metadata)
    def test_weekend(self):
        r=self.run_audit(['2026.11.13 23:00','2026.11.16 00:00'])
        self.assertEqual(r['slots_consistent_with_weekly_closure'],48)
        self.assertTrue(r['consistent_with_current_weekly_schedule'])
        self.assertEqual(r['unexplained_missing_slots'],0)
        for key in ('calendar_verified','historical_timezone_verified','holidays_verified','execution_authorized','boundary_coverage_verified'):self.assertFalse(r[key])
    def test_weekday_hole(self):
        r=self.run_audit(['2026.11.16 10:00','2026.11.16 12:00'])
        self.assertEqual(r['unexplained_missing_slots'],1);self.assertFalse(r['consistent_with_current_weekly_schedule'])
    def test_outside_session(self):
        r=self.run_audit(['2026.11.14 12:00']);self.assertEqual(len(r['candles_outside_current_sessions']),1)
    def test_no_current_candle(self):
        r=self.run_audit(['2026.11.16 18:00','2026.11.16 19:00'])
        self.assertEqual(r['closed_at_snapshot'],1);self.assertEqual(r['excluded_not_closed_at_snapshot'],1)
    def test_tail_missing_at_snapshot(self):
        r=self.run_audit(['2026.11.16 16:00'])
        self.assertEqual(r['trailing_unobserved_open_slots_at_snapshot'],2)
    def test_empty_sessions(self):
        self.m['quote_sessions_raw']=[]
        with self.assertRaises(ValueError):self.run_audit(['2026.11.16 18:00'])
    def test_symbol_exact(self):
        self.m['symbol']='EURUSD'
        with self.assertRaises(ValueError):self.run_audit(['2026.11.16 18:00'])
    def test_ambiguous_equal_session(self):
        with self.assertRaises(ValueError):sessions_from_rows([dict(weekday_mql=1,from_seconds_raw=0,to_seconds_raw=0)])
    def test_session_overlap(self):
        with self.assertRaises(ValueError):sessions_from_rows([dict(weekday_mql=1,from_seconds_raw=0,to_seconds_raw=500),dict(weekday_mql=1,from_seconds_raw=100,to_seconds_raw=600)])
    def test_sunday_overlap_monday(self):
        with self.assertRaises(ValueError):sessions_from_rows([dict(weekday_mql=0,from_seconds_raw=23*3600,to_seconds_raw=3600),dict(weekday_mql=1,from_seconds_raw=0,to_seconds_raw=86400)])
    def test_overnight(self):
        windows=sessions_from_rows([dict(weekday_mql=1,from_seconds_raw=23*3600,to_seconds_raw=3600)])
        self.assertTrue(intersects_session(datetime(2026,11,17,0),datetime(2026,11,17,1),windows))
        self.assertFalse(intersects_session(datetime(2026,11,17,1),datetime(2026,11,17,2),windows))
    def test_lunch_break(self):
        self.m['quote_sessions_raw']=[dict(weekday_mql=1,index=0,from_seconds_raw=9*3600,to_seconds_raw=12*3600),dict(weekday_mql=1,index=1,from_seconds_raw=13*3600,to_seconds_raw=18*3600)]
        r=self.run_audit(['2026.11.16 11:00','2026.11.16 13:00']);self.assertEqual(r['slots_consistent_with_weekly_closure'],1)
    def test_no_files_changed(self):
        self.run_audit(['2026.11.16 18:00']);before=[p.read_bytes() for p in (self.history,self.metadata)]
        audit_history(self.history,self.metadata);self.assertEqual(before,[p.read_bytes() for p in (self.history,self.metadata)])
    def test_misaligned(self):
        with self.assertRaises(ValueError):self.run_audit(['2026.11.16 18:30'])
    def test_duplicate_candle_rejected(self):
        self.run_audit(['2026.11.16 18:00']);self.history.write_text('{"EURUSD.pr_H1":{},"EURUSD.pr_H1":{}}')
        with self.assertRaises(ValueError):audit_history(self.history,self.metadata)
    def test_no_holiday_guess(self):
        r=self.run_audit(['2026.11.11 23:00','2026.11.13 00:00'])
        self.assertEqual(r['unexplained_missing_slots'],24)

if __name__=='__main__':unittest.main()
