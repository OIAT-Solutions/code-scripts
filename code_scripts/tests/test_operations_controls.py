from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from code_scripts.operations_controls import (ProposalQueue, payload_digest, require_posting_approval,
    reconcile_documents,purchase_units,settlement_match,create_night_offset,close_control,request_id)

class OperationTests(unittest.TestCase):
    def test_queue_duplicate_is_idempotent_changed_source_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=ProposalQueue(Path(tmp)/'queue.sqlite')
            rid=q.propose('A','Bill','vendor:invoice',{'total':100},['invoice.pdf'])
            self.assertEqual(rid,q.propose('A','Bill','vendor:invoice',{'total':100},['invoice.pdf']))
            with self.assertRaises(ValueError):q.propose('A','Bill','vendor:invoice',{'total':200},['invoice.pdf'])
            with q.connect() as db:
                self.assertEqual(db.execute('select count(*) from proposals').fetchone()[0],1)
                self.assertEqual(db.execute('select count(*) from audit').fetchone()[0],1)
    def test_approval_exact_and_expiring(self):
        now=datetime.now(timezone.utc);payload={'DocNumber':'SR-1','TxnDate':'2026-10-01'}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'approval.json'
            doc={'realm':'A','entity':'SalesReceipt','approved_by':'Reviewer','chat_approval_ref':'explicit yes',
                 'expires_at':(now+timedelta(hours=1)).isoformat(),'payload_sha256':[payload_digest(payload)]}
            p.write_text(json.dumps(doc));require_posting_approval(p,payload,'A','SalesReceipt',now)
            for args in ((p,{'DocNumber':'other'},'A','SalesReceipt',now),(p,payload,'B','SalesReceipt',now),
                         (p,payload,'A','SalesReceipt',now+timedelta(hours=2)),(None,payload,'A','SalesReceipt',now)):
                with self.assertRaises(ValueError):require_posting_approval(*args)
    def test_reconcile_same_key_wrong_amount_and_duplicates(self):
        row={'key':'SR-1','gross':'107.5','net':'100','vat':'7.5'}
        self.assertTrue(reconcile_documents([row],[row])['matched'])
        self.assertFalse(reconcile_documents([row],[{**row,'net':'90'}])['matched'])
        self.assertFalse(reconcile_documents([row],[row,row])['matched'])
        self.assertFalse(reconcile_documents([row],[])['matched'])
    def test_purchase_crates_use_same_bottle_units(self):
        p=purchase_units(2,24,24000)
        self.assertEqual(p['quantity'],48);self.assertEqual(p['unit_cost'],1000);self.assertEqual(p['amount'],48000)
    def test_settlement_requires_bank_evidence(self):
        with self.assertRaises(ValueError):settlement_match(100,2,3,95,statement_reference='')
        self.assertTrue(settlement_match(100,2,3,95,statement_reference='Bank statement line 3')['matched'])
    def test_create_offset_is_live_balance_plus_increase_minus_target(self):
        result=create_night_offset('142028049.94','150000000','150000000')
        self.assertEqual(result['credit_inventory_77'],'142028049.94')
        self.assertEqual(create_night_offset(100,150,140)['credit_inventory_77'],'110')
    def test_no_double_cogs_from_october(self):
        self.assertEqual(close_control('2026-09',100,120,purchases_already_expensed=True)['debit_inventory_77'],'20')
        with self.assertRaises(ValueError):close_control('2026-10',100,120,purchases_already_expensed=True)
    def test_request_ids_stable_and_scoped(self):
        self.assertEqual(request_id('A','SalesReceipt','SR-1'),request_id('A','SalesReceipt','SR-1'))
        self.assertNotEqual(request_id('A','SalesReceipt','SR-1'),request_id('B','SalesReceipt','SR-1'))

    def test_posting_result_requires_confirmed_id_and_preserves_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=ProposalQueue(Path(tmp)/'queue.sqlite')
            rid=q.propose('A','SalesReceipt','SR-1',{'total':100},['source.csv'])
            q.record_result(rid,'UNKNOWN',detail='timeout')
            with self.assertRaises(ValueError):q.record_result(rid,'POSTED')
            q.record_result(rid,'POSTED',qbo_id='receipt-123')
            with self.assertRaises(ValueError):q.record_result(rid,'FAILED')
            with q.connect() as db:
                self.assertEqual(db.execute('select state from proposals').fetchone()[0],'POSTED')
                self.assertEqual(db.execute('select count(*) from audit').fetchone()[0],3)
