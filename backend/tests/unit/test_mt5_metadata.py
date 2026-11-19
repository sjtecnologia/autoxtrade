import copy
import json
from pathlib import Path
import tempfile
import unittest
from trading.mt5_metadata import inspect_metadata


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'probe.json'
        self.data=dict(schema='autoxtrade.mt5.metadata.v1',read_only=True,orders_sent=0,symbol='EURUSD.pr',
            observed_utc_epoch=1800000000,last_server_wall_epoch=1800007200,
            estimated_server_wall_epoch=1800007200,tick_wall_epoch=1800007200,
            specifications=dict(point=.00001,tick_size=.00001,tick_value=1,contract_size=100000,volume_min=.01,volume_max=100,volume_step=.01),
            quote_sessions_raw=[dict(weekday_mql=1,index=0,from_seconds_raw=0,to_seconds_raw=86400)])
    def tearDown(self):self.temp.cleanup()
    def inspect(self):
        self.path.write_text(json.dumps(self.data));return inspect_metadata(self.path,now=1800000001)
    def test_current_offset_not_historical_authority(self):
        r=self.inspect();self.assertEqual(r['current_offset_candidate_seconds'],7200)
        for k in ('historical_timezone_verified','calendar_verified','execution_authorized','account_identity_verified'):self.assertFalse(r[k])
    def test_no_private_payload_export(self):
        self.data['account']='PRIVATE';self.assertNotIn('PRIVATE',str(self.inspect()))
    def test_no_mutation(self):
        self.path.write_text(json.dumps(self.data));before=self.path.read_bytes()
        inspect_metadata(self.path,now=1800000001);self.assertEqual(before,self.path.read_bytes())
    def test_duplicate_session(self):
        self.data['quote_sessions_raw']*=2
        with self.assertRaises(ValueError):self.inspect()
    def test_empty_sessions_not_24_7(self):
        self.data['quote_sessions_raw']=[];self.assertTrue(self.inspect()['warnings'])
    def test_malformed_clocks(self):
        for value in [True,float('nan'),0,None]:
            self.data['observed_utc_epoch']=value
            with self.assertRaises(ValueError):self.inspect()
    def test_bad_specs(self):
        self.data['specifications']['volume_min']=200
        self.assertFalse(self.inspect()['specifications_complete'])
    def test_stale_tick(self):
        self.data['tick_wall_epoch']-=500
        self.assertTrue(self.inspect()['warnings'])
    def test_schema_and_order_marker(self):
        self.data['orders_sent']=1
        with self.assertRaises(ValueError):self.inspect()
    def test_invalid_json(self):
        self.path.write_text('{')
        with self.assertRaises(ValueError):inspect_metadata(self.path)

if __name__=='__main__':unittest.main()
