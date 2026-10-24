"""Cross-process correctness and interruption cases; all data and services are synthetic."""
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from code_scripts.akponora_ops import common, catalogue_sync as cs, credit_invoices as ci, payments_sheet as ps
from code_scripts.akponora_ops import stock_follow as sf, ops_backup as backup, money_integrity as mi, uf_deposits as uf
from code_scripts.akponora_ops import daily_run as dr
from code_scripts.akponora_ops.work_state import WorkState
from code_scripts.tests.test_credit_invoices import Base, FakeQBO as CreditQBO
from code_scripts.tests.test_payments_sheet import FakeSheet, FakeQBO as PaymentQBO, pay
from code_scripts.tests import test_stock_follow as stock_tests
event = stock_tests.event
from code_scripts import credit_sales


class StateTests(unittest.TestCase):
    def test_work_updates_preserve_first_seen_and_atomic_file_failure_keeps_old_record(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); db = WorkState(root / 'work.sqlite3')
            db.put('bill', 'one', 'sha', 'review', {'owner': 'OIAT'})
            first = db.get('bill', 'one')['first_seen']
            db.put('bill', 'one', 'sha', 'verified', {})
            self.assertEqual(db.get('bill', 'one')['first_seen'], first)
            self.assertEqual(db.unresolved('bill'), [])
            path = root / 'record.json';common.dump_json(path, {'complete': True})
            with mock.patch.object(common.os, 'replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):common.dump_json(path, {'complete': False})
            self.assertEqual(json.loads(path.read_text()), {'complete': True})
            self.assertEqual(list(root.glob('.*.tmp')), [])


class InvoiceHardeningTests(Base):
    def setUp(self):
        super().setUp();credit_sales.split_raw(str(self.raw), '2026-10-05', state_root=self.root)
        self.qbo = CreditQBO([{'Id': '62', 'DisplayName': 'GPFH', 'Active': True},
                              {'Id': '311', 'DisplayName': 'JIFA FELIX', 'Active': True}])

    def test_existing_wrong_customer_duplicate_number_or_changed_line_is_held(self):
        self.run_ci(self.qbo);before = len(self.qbo.posts)
        original = json.loads(json.dumps(self.qbo.invoices))
        for change in ('customer', 'duplicate', 'line'):
            self.qbo.invoices = json.loads(json.dumps(original))
            target = self.qbo.invoices[0]
            if change == 'customer':target['CustomerRef'] = {'value': 'wrong'}
            elif change == 'duplicate':self.qbo.invoices.append({**target, 'Id': 'duplicate'})
            else:target['Line'][0]['SalesItemLineDetail']['Qty'] = 99
            result = self.run_ci(self.qbo)
            self.assertTrue(any(x['status'] == ci.HELD for x in result['invoices']), change)
            self.assertEqual(len(self.qbo.posts), before)

    def test_prepared_work_is_reused_but_live_changed_total_is_still_refused(self):
        mapping = self.root / 'approved.csv';mapping.write_text('approved mapping fingerprint')
        self.config.product_conversion_file = mapping
        self.run_ci(self.qbo)
        with mock.patch.object(ci, 'invoice_lines', side_effect=AssertionError('unexpected retransform')):
            result = self.run_ci(self.qbo)
            self.assertEqual(result['reused_prepared'], 3)
            self.qbo.invoices[0]['TotalAmt'] = 1
            result = self.run_ci(self.qbo)
            self.assertTrue(any(x['status'] == ci.HELD for x in result['invoices']))


class IdentifiedSheet(FakeSheet):
    ensure_payment_ids = ps.GoogleSheet.ensure_payment_ids
    def write_cells(self, updates):
        ordinary = []
        for rng, values in updates:
            tab, cell = rng.rsplit('!', 1);tab = tab.strip("'")
            if cell.startswith('K'):
                n = int(cell[1:]);row = self.tabs[tab][n - 1]
                row += [''] * (11 - len(row));row[10] = values[0]
            else:ordinary.append((rng, values))
        super().write_cells(ordinary)


class RecoverableQBO(PaymentQBO):
    def __init__(self):
        super().__init__();self.payments = [];self.interrupt = False
    def query_all(self, sql, entity):
        if entity in ('Payment', 'BillPayment'):
            return [x for x in self.payments if x['_entity'] == entity]
        return super().query_all(sql, entity)
    def post_json(self, path, body, requestid):
        response = super().post_json(path, body, requestid)
        entity = 'Payment' if path == '/payment' else 'BillPayment'
        self.payments.append({**body, 'Id': response.json()[entity]['Id'], '_entity': entity})
        if self.interrupt:
            self.interrupt = False
            raise ConnectionError('response lost after acceptance')
        return response


class PaymentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(ps, 'state_dir', return_value=Path(self.temp.name));patch.start();self.addCleanup(patch.stop)
        patch = mock.patch.object(ps, 'today', return_value='2026-10-10');patch.start();self.addCleanup(patch.stop)
        self.qbo = RecoverableQBO();self.env = {ps.SHEET_ENV: 'fake', ps.POST_ENV: '1'}
    def run_sheet(self, sheet):
        return ps.run(client=self.qbo, write_client=self.qbo, sheet=sheet, env=self.env)

    def test_accepted_full_payment_with_lost_response_is_recovered_without_second_post(self):
        sheet = IdentifiedSheet(credit=[pay('CR261005-389764', 58200)])
        self.qbo.interrupt = True
        first = self.run_sheet(sheet)
        self.assertEqual(first['credit'][0]['status'], 'failed')
        self.assertEqual(self.qbo.invoices['80642']['Balance'], 0)
        second = self.run_sheet(sheet)
        self.assertTrue(second['credit'][0]['recovered'])
        self.assertEqual(len(self.qbo.posts), 1)
        self.assertTrue(sheet.tabs[ps.CREDIT_PAYMENTS][1][10].startswith('NP-'))

    def test_distinct_identical_partial_payments_keep_ids_through_sort_and_edits(self):
        sheet = IdentifiedSheet(credit=[pay('CR261005-389764', 10000), pay('CR261005-389764', 10000)])
        self.run_sheet(sheet)
        rows = sheet.tabs[ps.CREDIT_PAYMENTS]
        self.assertNotEqual(rows[1][10], rows[2][10])
        rows[1], rows[2] = rows[2], rows[1]
        self.run_sheet(sheet);self.assertEqual(len(self.qbo.posts), 2)
        rows[1][1] = 5000;rows[1][7] = ''
        result = self.run_sheet(sheet)
        self.assertTrue(any(x['status'] == 'edited after posting' for x in result['credit']))
        self.assertEqual(len(self.qbo.posts), 2)

    def test_copied_payment_identity_fails_before_any_post(self):
        sheet = IdentifiedSheet(credit=[pay('CR261005-389764', 10000), pay('CR261005-389764', 10000)])
        for row in sheet.tabs[ps.CREDIT_PAYMENTS][1:]:row += [''] * (11-len(row));row[10] = 'NP-' + 'a'*20
        with self.assertRaises(ps.SheetError):self.run_sheet(sheet)
        self.assertEqual(self.qbo.posts, [])

    def test_status_updates_follow_identity_after_sort_and_edited_source_stops(self):
        sheet=IdentifiedSheet(credit=[pay('CR261005-389764',10000),pay('CR261005-389764',5000)])
        rows=sheet.ensure_payment_ids(ps.CREDIT_PAYMENTS,sheet.read(ps.CREDIT_PAYMENTS,11))
        sheet.tabs[ps.CREDIT_PAYMENTS][1:]=reversed(sheet.tabs[ps.CREDIT_PAYMENTS][1:])
        moved=ps.relocate_updates(sheet,ps.CREDIT_PAYMENTS,rows,[(2,['Posted','1','now'])])
        self.assertEqual(moved[0][0],3)
        sha='different source hash'
        with self.assertRaises(ps.SheetError):ps.current_payment_row(sheet,ps.CREDIT_PAYMENTS,rows[1][10],sha)


class StockHardeningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = stock_tests.StockFollowTests('test_rules');self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.tmp, self.qbo, self.s = self.fixture.tmp, self.fixture.qbo, self.fixture.s

    def test_many_small_lines_in_large_transfer_wait_together(self):
        a, b = event('a', value='80000'), event('b', value='80000', item='102')
        b['transfer_id'] = a['transfer_id']
        events = sf.collect([self.fixture.capture([a,b])])
        result = sf.plan(events, self.tmp/'p', s=self.s, approve_over_cap=False)
        self.assertEqual((result['events'], result['review_count']), (0,2))
        result = sf.plan(events, self.tmp/'approved', s=self.s, approve_over_cap=True)
        self.assertEqual(result['events'],2)

    def test_posted_movement_edit_is_visible_and_never_posted_again(self):
        a = event('a');capture = self.fixture.capture([a]);events=sf.collect([capture])
        p = sf.plan(events,self.tmp/'p',s=self.s,approve_over_cap=False)
        sf.post(self.tmp/'p',client=self.qbo,approval_ref='test',expect_sha=p['payloads_sha256'])
        a['canonical_qty_change']='99';events=sf.collect([self.fixture.capture([a])])
        result=sf.plan(events,self.tmp/'again',s=self.s,approve_over_cap=False)
        self.assertEqual((result['events'],result['review_count']),(0,1))
        self.assertEqual(len(self.qbo.posts),1)

    def test_incomplete_capture_cannot_post_partial_stock(self):
        p=self.fixture.capture([event('a')]);doc=json.loads(p.read_text());doc['errors']=['missing page'];p.write_text(json.dumps(doc))
        result=sf.nightly(p,self.tmp/'night',write_client=self.qbo,env={})
        self.assertIn('incomplete',result['note']);self.assertEqual(self.qbo.posts,[])

    def test_accepted_stock_response_lost_is_recovered_by_original_document(self):
        events = sf.collect([self.fixture.capture([event('a')])])
        plan = sf.plan(events, self.tmp/'p', s=self.s, approve_over_cap=False)
        original = self.qbo.post_json
        def lost(*args):
            original(*args)
            raise ConnectionError('response lost')
        with mock.patch.object(self.qbo, 'post_json', side_effect=lost):
            with self.assertRaises(ConnectionError):sf.post(self.tmp/'p', client=self.qbo, approval_ref='test', expect_sha=plan['payloads_sha256'])
        journal = WorkState(self.tmp/'state'/sf.TOOL/'work.sqlite3')
        self.assertEqual(sf.recover_stock_operations(self.qbo, journal), [])
        self.assertEqual(len(sf.ledger()), 1)
        sf.post(self.tmp/'p', client=self.qbo, approval_ref='test', expect_sha=plan['payloads_sha256'])
        self.assertEqual(len(self.qbo.posts), 1)

    def test_delivery_overlap_holds_positive_count_change(self):
        folder=self.tmp/'bills';folder.mkdir()
        common.dump_json(folder/'plan.json', {'entries':[{'po':{'ref':'12','received_date':'2026-10-08'}}]})
        common.write_csv(folder/'review_lines.csv',[{'PO':'12','QBO Item Id':'101','QBO Qty':'20'}],['PO','QBO Item Id','QBO Qty'])
        events=sf.delivery_overlaps(sf.collect([self.fixture.capture([event('a')])]),folder)
        self.assertIn('possible delivery', sf.classify(next(iter(events.values())),self.s['cap'])[1])


class MoneyAndReceiptTests(unittest.TestCase):
    def test_cancelled_zero_receipt_does_not_need_details_but_contradictory_receipt_does(self):
        cancelled={'OrderRef': 1,'StatusName':'Cancelled','DateReceived':None,'TotalValueReceived':0}
        self.assertFalse(cs.receipt_candidate(cancelled))
        self.assertTrue(cs.receipt_candidate({**cancelled,'TotalValueReceived':10}))
        self.assertNotIn('_missing_details',cs.receipts_from_orders([cancelled],{},[]))
    def test_repayment_and_positive_till_overage_require_review(self):
        rows=[ps.CREDIT_PAY_COLS,pay('CR1',5000)]
        days=mi.repayment_days(rows,parse_day=ps.parse_day,parse_money=ps.money)
        self.assertIsNotNone(mi.overage_overlap('2026-10-06',Decimal(5000),days))
        self.assertIsNone(mi.overage_overlap('2026-10-06',Decimal(0),days))
    def test_missing_counts_are_unavailable_and_actual_cash_explains_movements(self):
        self.assertEqual(mi.check_cash_count({})['status'],'needs evidence')
        record={'date':'2026-10-06','counted_by':'test','opening_float':100,'cash_sales':1000,'cash_repayments':200,
                'supplier_cash_payments':300,'cash_banked':500,'other_approved_outflows':0,'closing_count':500}
        self.assertEqual(mi.check_cash_count(record)['status'],'matches')
        record['closing_count']=450
        self.assertEqual(mi.check_cash_count(record)['difference'],'-50')

    def test_changed_till_or_repayment_input_stops_banking_before_client_use(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder);(out/'payloads.jsonl').write_text('')
            sha=common.sha256_file(out/'payloads.jsonl')
            common.dump_json(out/'summary.json', {'payloads_sha256':sha,'realm':uf.REALM,'status':uf.READY,
                'day':'2026-10-06','receipts_total':'100','sheet_day_sha256':'original'})
            s={'auto':True,'cap':Decimal(1000),'_till_source':object()}
            with mock.patch.object(uf.till_sheet,'fresh_day',return_value=({'date':'2026-10-06'},'', '')):
                with self.assertRaisesRegex(uf.StopRun,'till day changed'):
                    uf.post_day(out,client=object(),approval_ref='test',expect_sha=sha,auto=True,s=s)
            s={'auto':True,'cap':Decimal(1000),'repayments_by_day':{},'_repayment_reader':lambda:{'2026-10-06':{'amount':1}}}
            with self.assertRaisesRegex(uf.StopRun,'repayment input changed'):
                uf.post_day(out,client=object(),approval_ref='test',expect_sha=sha,auto=True,s=s)


class FinalVerificationTests(unittest.TestCase):
    @unittest.skipIf(dr.os.name == 'nt', 'production collectors run in Linux containers')
    def test_collector_deadline_stops_its_process_group_but_never_financial_writer(self):
        for module, readonly in [('stock_movements',True),('bills_sync',False)]:
            with tempfile.TemporaryDirectory() as folder:
                proc=mock.Mock(pid=12345)
                proc.wait.side_effect=[dr.subprocess.TimeoutExpired('test',30),0]
                with mock.patch.object(dr.subprocess,'Popen',return_value=proc), \
                     mock.patch.object(dr.time,'monotonic',side_effect=[0,100]), \
                     mock.patch.object(dr.os,'killpg') as kill:
                    result=dr.run_command(['python','-m','code_scripts.akponora_ops.'+module],
                        log_path=Path(folder)/'log.txt',env={'OIAT_READ_STEP_TIMEOUT_SECONDS':'60'})
                self.assertEqual(result,2 if readonly else 0)
                self.assertEqual(kill.call_count,1 if readonly else 0)
                heartbeat=json.loads((Path(folder)/'heartbeat.json').read_text())
                self.assertEqual(heartbeat['safe_to_stop'],readonly)

    def test_failed_final_snapshot_preserves_run_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            run=dr.DailyRun('2026-10-06',root=Path(folder),dry_run=True,send_slack=False)
            run.run_dir.mkdir(parents=True);run.started_at=dr._now_utc()
            run.results=[dr.StepResult(name='stock_follow',status=dr.OK,counts={'posted':[{'status':'POSTED'}]})]
            with mock.patch.object(run,'run_step',side_effect=RuntimeError('read failed')):
                result=run.finish()
            self.assertEqual(result['exit_code'],dr.EXIT_FAILED)
            self.assertTrue((run.run_dir/'summary.json').exists())


class EncryptedRecoveryTests(unittest.TestCase):
    def test_consistent_sqlite_incremental_backup_restore_and_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);state=root/'state';state.mkdir();key=root/'key';backup.init_key(key)
            db=state/'db.sqlite3'
            with sqlite3.connect(db) as c:c.execute('CREATE TABLE test (value TEXT)');c.execute("INSERT INTO test VALUES ('evidence')")
            source=state/'mapping.csv';source.write_text('approved identity')
            first=backup.create(state,root/'backups',key,files=[db,source])
            second=backup.create(state,root/'backups',key,files=[db,source])
            self.assertEqual(second['reused'],1)
            third=backup.create(state,root/'backups',key,files=[db,source])
            retention=backup.retain(Path(third['manifest']),key,daily=2,monthly=1)
            self.assertEqual(retention['sets_kept'],2)
            result=backup.restore(Path(second['manifest']),key,root/'restore')
            self.assertEqual(result['files_restored'],2)
            with sqlite3.connect(root/'restore/db.sqlite3') as c:self.assertEqual(c.execute('SELECT value FROM test').fetchone()[0],'evidence')
            offsite=backup.copy_offsite(Path(third['manifest']),root/'offsite',key)
            self.assertTrue(offsite['ciphertext_copy_verified'])
            manifest=backup.read_manifest(Path(second['manifest']),backup.read_key(key))
            entry=manifest['files']['mapping.csv'];blob=Path(second['manifest']).parent.parent/'blobs'/(entry['sha256']+'.enc')
            data=bytearray(blob.read_bytes());data[-1]^=1;blob.write_bytes(data)
            with self.assertRaises(ValueError):backup.restore(Path(second['manifest']),key,root/'bad_restore')
            with self.assertRaises(FileExistsError):backup.init_key(key)
