"""Synthetic user-confirmation and daily-run contracts; never use real ledger."""
from datetime import datetime,timezone
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
from . import http_api,daily
from .core import Ledger

class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.ledger=Ledger(self.root/'ledger')
        self.hit={'file':'reference.md','source_sha256':'a'*64,'content_hash':'b'*64,'start':2,'end':4,'heading':'Synthetic chapter','pages':['3']}
        http_api.bind(lambda q,k:[self.hit],self.root)
        self.event={'event_id':'synthetic-browse-001','occurred_at':datetime.now(timezone.utc).isoformat(),'action':'browse','confirmation':'viewed_this_excerpt','query':'Synthetic topic','source_ref':{k:self.hit[k] for k in ('file','source_sha256','content_hash')}}
        self.mock=patch.object(http_api,'Ledger',lambda:self.ledger);self.mock.start()
    def tearDown(self):self.mock.stop();self.tmp.cleanup()
    def test_explicit_action_records_only_browse_and_retries_same_id(self):
        code,result=http_api.handle_post('/api/study/events',self.event);self.assertEqual(code,200);self.assertTrue(result['inserted']);self.assertFalse(result['mastery_assessed'])
        _,again=http_api.handle_post('/api/study/events',self.event);self.assertFalse(again['inserted']);self.assertEqual(self.ledger.status()['event_count'],1)
    def test_no_confirmation_or_agent_payload_cannot_record(self):
        for changes in [{'confirmation':None},{'actor':'agent'},{'action':'study'}]:
            code,_=http_api.handle_post('/api/study/events',{**self.event,**changes});self.assertEqual(code,400)
        self.assertEqual(self.ledger.status()['event_count'],0)
    def test_dry_run_does_not_create_confirmed_ledger(self):
        code,result=http_api.handle_post('/api/study/events',{**self.event,'dry':True});self.assertEqual(code,200);self.assertFalse(result['recorded']);self.assertFalse(self.ledger.db_path.exists())
    def test_changed_source_and_unknown_route_fail_closed(self):
        event={**self.event,'source_ref':{**self.event['source_ref'],'content_hash':'c'*64}}
        self.assertEqual(http_api.handle_post('/api/study/events',event)[0],409);self.assertEqual(http_api.handle_post('/api/unit/save',self.event)[0],404)
    def test_missing_timezone_is_rejected(self):
        self.assertEqual(http_api.handle_post('/api/study/events',{**self.event,'occurred_at':'2026-11-03'})[0],400)
    def test_ledger_conflict_does_not_overwrite(self):
        http_api.handle_post('/api/study/events',self.event)
        self.assertEqual(http_api.handle_post('/api/study/events',{**self.event,'query':'Another synthetic topic'})[0],409)
    def test_offline_sync_still_distills_existing_confirmed_events(self):
        http_api.handle_post('/api/study/events',self.event)
        with patch.object(daily,'sync',side_effect=OSError):result=daily.run(self.ledger.home)
        self.assertEqual(result['sync']['status'],'unavailable');self.assertEqual(result['distillation']['status'],'complete');self.assertEqual(self.ledger.status()['summary_count'],1)
    def test_no_events_never_creates_summary_when_source_offline(self):
        with patch.object(daily,'sync',side_effect=OSError):result=daily.run(self.ledger.home)
        self.assertEqual(result['distillation']['status'],'no_events');self.assertFalse(self.ledger.db_path.exists())

if __name__=='__main__':unittest.main()