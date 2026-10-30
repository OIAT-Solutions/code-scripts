"""Sunday orchestration through the real capture guard/index/normalizer; no network or QBO."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops import daily_run as dr, stock_movements as sm, epos_adjustments as reader



_CURSOR_DIR = None


def setUpModule():  # noqa: N802 - unittest hook: the stock-movement "last run" marker never touches real state
    import tempfile as _tf
    from unittest import mock as _mock

    global _CURSOR_DIR
    _CURSOR_DIR = _tf.TemporaryDirectory()
    _p = _mock.patch("code_scripts.akponora_ops.daily_run.movement_cursor_path",
                     lambda: __import__("pathlib").Path(_CURSOR_DIR.name) / "cursor.json")
    _p.start()


def tearDownModule():  # noqa: N802
    from unittest import mock as _mock

    _mock.patch.stopall()
    _CURSOR_DIR.cleanup()

class AuditWindowTests(unittest.TestCase):
    @staticmethod
    def empty_capture(path, start, sources, company, *, through_date):
        assert sources == 'both' and company == 'company_a'
        assert 0 <= (date.fromisoformat(through_date) - date.fromisoformat(start)).days < 7
        for name in ('stocktakes', 'stockmovements'):
            (path / f'epos_{name}_list.json').write_text('[]')
        (path/'capture_metrics.json').write_text(json.dumps({'window':[start,through_date]}))

    def test_weekly_timeout_covers_each_bounded_section_without_changing_daily_limit(self):
        cmd=['python','-m','code_scripts.akponora_ops.stock_movements','--weekly-audit',
             '--from-date','2026-10-01','--through-date','2026-10-25']
        self.assertEqual(dr.read_step_limit(cmd,{}),4800)
        self.assertEqual(dr.read_step_limit([x for x in cmd if x!='--weekly-audit'],{}),1200)
        self.assertEqual(dr.read_step_limit(cmd,{'OIAT_READ_STEP_TIMEOUT_SECONDS':'90'}),360)

    def test_resume_copies_recent_complete_historical_sections_but_rereads_recent_stock(self):
        with tempfile.TemporaryDirectory() as folder:
            prior=sm.collect_audit(Path(folder)/'prior','2026-10-01','2026-10-25',reader=self.empty_capture)
            original=(prior/'audit_index.json').read_bytes();calls=[]
            def capture(*args,**kw):
                calls.append((args[1],kw['through_date']));self.empty_capture(*args,**kw)
            resumed=sm.collect_audit(Path(folder)/'recovery','2026-10-01','2026-10-26',reader=capture,resume_from=prior)
            self.assertEqual(calls,[('2026-10-22','2026-10-26')])
            self.assertEqual(sm.read_capture(resumed)[1],[])
            self.assertEqual((prior/'audit_index.json').read_bytes(),original)
            index=json.loads((resumed/'audit_index.json').read_text())
            self.assertEqual(sum(bool(c.get('resumed_observation')) for c in index['chunks']),3)

    def test_resume_rejects_stale_or_incomplete_historical_evidence(self):
        for kind in ('old_index','old_capture','missing_source','wrong_window','unsafe_path'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as folder:
                prior=sm.collect_audit(Path(folder)/'prior','2026-10-01','2026-10-25',reader=self.empty_capture)
                index=json.loads((prior/'audit_index.json').read_text());child=prior/index['chunks'][0]['path']
                if kind=='old_index':os.utime(prior/'audit_index.json',(0,0))
                if kind=='old_capture':os.utime(child/'capture_metrics.json',(0,0))
                if kind=='missing_source':(child/'epos_stockmovements_list.json').unlink()
                if kind=='wrong_window':(child/'capture_metrics.json').write_text('{"window":["2026-10-02","2026-10-07"]}')
                if kind=='unsafe_path':
                    index['chunks'][0]['path']='../outside';(prior/'audit_index.json').write_text(json.dumps(index))
                with self.assertRaises(ValueError):
                    sm.collect_audit(Path(folder)/'recovery','2026-10-01','2026-10-25',reader=self.empty_capture,resume_from=prior)

    def test_resume_does_not_reuse_incomplete_historical_section(self):
        with tempfile.TemporaryDirectory() as folder:
            prior=sm.collect_audit(Path(folder)/'prior','2026-10-01','2026-10-25',reader=self.empty_capture)
            index=json.loads((prior/'audit_index.json').read_text());index['chunks'][1]['status']='pending'
            (prior/'audit_index.json').write_text(json.dumps(index));calls=[]
            def capture(*args,**kw):calls.append(args[1]);self.empty_capture(*args,**kw)
            sm.collect_audit(Path(folder)/'recovery','2026-10-01','2026-10-25',reader=capture,resume_from=prior)
            self.assertEqual(calls,['2026-10-08','2026-10-22'])

    def windows_for(self, today, cursor=None):
        seen = []
        if cursor:
            dr.save_movement_cursor(date.fromisoformat(cursor))
        else:
            dr.movement_cursor_path().unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);catalogue = root/'catalogue.json';catalogue.write_text('[]')
            mapping = root/'mapping.csv';mapping.write_text('EPOS Product ID,Review Status\n')
            def run_command(cmd, *, log_path, env):
                seen.append((cmd[cmd.index('--from-date')+1], cmd[cmd.index('--through-date')+1], '--weekly-audit' in cmd))
                return 0
            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):return datetime.fromisoformat(today).replace(hour=18, tzinfo=ZoneInfo('Africa/Lagos'))
            run = dr.DailyRun('2026-11-21',root=root/'daily',runner=run_command,send_slack=False,env={})
            with mock.patch.object(dr,'datetime',Clock):
                run.stock_movement_review(dr.StepResult(name='stock'),{'sources':{'catalogue':{'path':str(catalogue)},'mapping':{'path':str(mapping)}}})
        return seen[0]

    def test_reads_since_the_last_clean_capture_and_no_sunday_wide_audit(self):
        # owner 26 Oct 2026: completed EPOS stock takes are view-only, so only new changes are read
        self.assertEqual(self.windows_for('2026-11-22', cursor='2026-11-21'), ('2026-11-21', '2026-11-22', False))  # Sunday too
        self.assertEqual(self.windows_for('2026-11-18'), ('2026-11-16', '2026-11-18', False))  # no marker yet: 3 days

    def test_a_missed_week_catches_up_in_bounded_sections(self):
        self.assertEqual(self.windows_for('2026-11-22', cursor='2026-11-10'), ('2026-11-10', '2026-11-22', True))

    def test_audit_resume_refuses_full_financial_routine(self):
        with self.assertRaises(ValueError):dr.DailyRun('2026-10-24',stock_audit_resume=Path('/missing'))

    def test_sunday_daily_command_runs_actual_bounded_collector(self):
        # 26 Oct 2026: the wide read now only happens as a catch-up after missed nights (here: last clean 1 Oct)
        dr.save_movement_cursor(date(2026, 10, 1))
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);catalogue = root/'catalogue.json';catalogue.write_text('[]')
            mapping = root/'mapping.csv';mapping.write_text('EPOS Product ID,Review Status\n')
            def capture(*args, **kw):
                calls.append((args[1], kw['through_date']))
                self.empty_capture(*args, **kw)
            def run_command(cmd, *, log_path, env):
                self.assertIn('--weekly-audit', cmd)
                return sm.main(cmd[cmd.index('code_scripts.akponora_ops.stock_movements')+1:])
            class Sunday(datetime):
                @classmethod
                def now(cls, tz=None):return datetime(2026,10,25,18,tzinfo=ZoneInfo('Africa/Lagos'))
            run = dr.DailyRun('2026-10-24',root=root/'daily',runner=run_command,send_slack=False,env={})
            result = dr.StepResult(name='stock')
            with mock.patch.object(dr,'datetime',Sunday), mock.patch.object(reader,'scrape_adjustments',side_effect=capture):
                run.stock_movement_review(result,{'sources':{'catalogue':{'path':str(catalogue)},'mapping':{'path':str(mapping)}}})
            report=json.loads((run.step_dir('movements')/'summary.json').read_text())
            self.assertEqual(report['errors'],[])
            self.assertEqual(report['status'],'NO_CAPTURED_ADJUSTMENTS')
            self.assertEqual(calls,[('2026-10-01','2026-10-07'),('2026-10-08','2026-10-14'),
                                    ('2026-10-15','2026-10-21'),('2026-10-22','2026-10-25')])
            class Monday(datetime):
                @classmethod
                def now(cls,tz=None):return datetime(2026,10,26,0,15,tzinfo=ZoneInfo('Africa/Lagos'))
            resumed=dr.DailyRun('2026-10-24',root=root/'recovery',runner=run_command,send_slack=False,env={},
                                only=['stock','stock_follow'],stock_audit_resume=Path(report['sources']['captures']))
            with mock.patch.object(dr,'datetime',Monday),mock.patch.object(reader,'scrape_adjustments',side_effect=capture):
                resumed.stock_movement_review(dr.StepResult(name='stock'),{'sources':{'catalogue':{'path':str(catalogue)},'mapping':{'path':str(mapping)}}})
            self.assertEqual(calls[4:],[('2026-10-22','2026-10-26')])
            self.assertEqual(json.loads((resumed.step_dir('movements')/'summary.json').read_text())['errors'],[])

    def test_windows_are_disjoint_and_cover_cross_month_range(self):
        windows=sm.capture_windows('2026-10-28','2026-11-07')
        self.assertEqual(windows,[('2026-10-28','2026-11-03'),('2026-11-04','2026-11-07')])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):sm.collect(Path(folder),'2026-10-01','2026-10-25',reader=self.empty_capture)

    def test_missing_chunk_source_or_gap_in_index_stays_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            root=sm.collect_audit(Path(folder),'2026-10-01','2026-10-25',reader=self.empty_capture)
            index=json.loads((root/'audit_index.json').read_text())
            (root/index['chunks'][1]['path']/'epos_stockmovements_list.json').unlink()
            self.assertTrue(sm.read_capture(root)[1])
            index['chunks'].pop(1);(root/'audit_index.json').write_text(json.dumps(index))
            self.assertIn('does not cover', ' '.join(sm.read_capture(root)[1]))

    def test_reader_failure_keeps_completed_and_unattempted_chunks_visible(self):
        calls=[]
        def fail_second(*args,**kw):
            calls.append(args[1])
            if len(calls)==2:raise RuntimeError('synthetic source failure')
            self.empty_capture(*args,**kw)
        with tempfile.TemporaryDirectory() as folder:
            root=sm.collect_audit(Path(folder),'2026-10-01','2026-10-25',reader=fail_second)
            errors=sm.read_capture(root)[1]
            self.assertTrue(any('source failure' in e for e in errors))
            self.assertTrue(any('2026-10-22' in e and 'not completed' in e for e in errors))
            self.assertEqual(len(calls),2)

    def test_reversed_or_pre_cutover_audit_refuses_before_source_reads(self):
        for start,end in [('2026-09-30','2026-10-01'),('2026-10-25','2026-10-01')]:
            with self.assertRaises(ValueError):sm.capture_windows(start,end)

    def test_stock_only_recovery_requires_read_only_delivery_refresh_before_followup(self):
        from code_scripts.akponora_ops import stock_follow as sf
        for code,missing in ((0,0),(2,0),(0,1),(0,None)):
            with self.subTest(code=code,missing=missing), tempfile.TemporaryDirectory() as folder:
                calls=[]
                def runner(cmd,**kw):
                    calls.append(cmd)
                    evidence=Path(folder)/run.run_dir.name/'stock_receipts/evidence'
                    evidence.mkdir(parents=True,exist_ok=True)
                    (evidence/'capture_metrics.json').write_text(json.dumps({'missing_details':missing}))
                    return code
                run=dr.DailyRun('2026-10-24',root=Path(folder),only=['stock','stock_follow'],runner=runner,
                                send_slack=False,env={sf.POST_ENV:'0'})
                (run.step_dir('movements')/'summary.json').write_text('{"company":"company_a","events":[]}')
                result=dr.StepResult(name='stock_follow',out=str(run.step_dir('stock_follow')))
                with mock.patch.object(sf,'nightly',return_value={'posted':[],'note':'synthetic check'}) as follow:
                    run.step_stock_follow(result)
                self.assertIn('plan',calls[0])
                self.assertIn('code_scripts.akponora_ops.bills_sync',calls[0])
                if code or missing != 0:
                    follow.assert_not_called();self.assertEqual(result.status,dr.FAILED)
                else:
                    self.assertEqual(follow.call_args.kwargs['bills_dir'],run.step_dir('stock_receipts'))
