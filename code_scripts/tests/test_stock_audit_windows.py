"""Sunday orchestration through the real capture guard/index/normalizer; no network or QBO."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops import daily_run as dr, stock_movements as sm, epos_adjustments as reader


class AuditWindowTests(unittest.TestCase):
    @staticmethod
    def empty_capture(path, start, sources, company, *, through_date):
        assert sources == 'both' and company == 'company_a'
        assert 0 <= (date.fromisoformat(through_date) - date.fromisoformat(start)).days < 7
        for name in ('stocktakes', 'stockmovements'):
            (path / f'epos_{name}_list.json').write_text('[]')

    def test_sunday_daily_command_runs_actual_bounded_collector(self):
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
        for code in (0,2):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                calls=[]
                def runner(cmd,**kw):calls.append(cmd);return code
                run=dr.DailyRun('2026-10-24',root=Path(folder),only=['stock','stock_follow'],runner=runner,
                                send_slack=False,env={sf.POST_ENV:'0'})
                (run.step_dir('movements')/'summary.json').write_text('{"company":"company_a","events":[]}')
                result=dr.StepResult(name='stock_follow',out=str(run.step_dir('stock_follow')))
                with mock.patch.object(sf,'nightly',return_value={'posted':[],'note':'synthetic check'}) as follow:
                    run.step_stock_follow(result)
                self.assertIn('plan',calls[0])
                self.assertIn('code_scripts.akponora_ops.bills_sync',calls[0])
                if code:
                    follow.assert_not_called();self.assertEqual(result.status,dr.FAILED)
                else:
                    self.assertEqual(follow.call_args.kwargs['bills_dir'],run.step_dir('stock_receipts'))
