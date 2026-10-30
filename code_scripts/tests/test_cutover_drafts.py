import unittest
from code_scripts.cutover_drafts import catalogue_drafts,bill_draft
from code_scripts.operations_controls import assert_receipt_matches,require_reconciliation_match,assert_no_posting_hold
from unittest import mock
from pathlib import Path
import tempfile

class DraftTests(unittest.TestCase):
    def family(self):
        return dict(family_key='wine',name='Wine bottle',sku='AKP-WINE',canonical_unit='bottle',stock_owner_id='123',
                    approved_by='Staff',approval_ref='unit decision',unit_evidence='master export',cost_evidence='invoice',cost_tax_basis='exclusive',
                    income_account_id='income',cogs_account_id='76',tax_code_id='2',full_multiplier='24',loose_multiplier='1',
                    purchase_multiplier='24',canonical_unit_cost='1000',qbo_item_id='90001')
    def count(self,full='2',loose='3'):
        return dict(stock_owner_id='123',source_ref='final_count.csv:2',cutoff_date='2026-09-30',full_count=full,loose_count=loose)
    def test_full_plus_loose_and_cost_same_unit(self):
        out=catalogue_drafts([self.family()],[self.count()],cutoff_date='2026-09-30')
        self.assertEqual(out['proposed_value'],'51000')
        self.assertEqual(out['drafts'][0]['payload']['QtyOnHand'],51)
    def test_rejects_provisional_cutoff_and_duplicate_stock(self):
        with self.assertRaises(ValueError):catalogue_drafts([self.family()],[self.count()],cutoff_date='2026-09-16')
        with self.assertRaises(ValueError):catalogue_drafts([self.family()],[self.count(),self.count()],cutoff_date='2026-09-30')
        with self.assertRaises(ValueError):catalogue_drafts([{**self.family(),'approved_by':''}],[self.count()],cutoff_date='2026-09-30')
    def test_negative_and_missing_cost_flags(self):
        out=catalogue_drafts([self.family()],[self.count('-2','0')],cutoff_date='2026-09-30')
        self.assertEqual(out['drafts'][0]['quantity'],'0');self.assertIn('NEGATIVE_ZERO_FLOOR',out['drafts'][0]['flags'])
        out=catalogue_drafts([{**self.family(),'canonical_unit_cost':'0'}],[self.count()],cutoff_date='2026-09-30')
        self.assertIn('MISSING_COST_POSITIVE_STOCK',out['drafts'][0]['flags'])
    def test_bill_uses_same_exact_inventory_id_and_canonical_units(self):
        live=dict(Id='90001',Name='Wine bottle',Sku='AKP-WINE',Type='Inventory',Active=True,TrackQtyOnHand=True,InvStartDate='2026-10-01',AssetAccountRef={'value':'77'})
        out=bill_draft(self.family(),live,vendor_id='55',invoice_no='INV-1',invoice_date='2026-10-01',packs=2,cost_per_pack=24000,invoice_ref='invoice.pdf',goods_received_ref='GRN-1')
        detail=out['payload']['Line'][0]['ItemBasedExpenseLineDetail']
        self.assertEqual(detail['Qty'],48);self.assertEqual(detail['UnitPrice'],1000);self.assertEqual(detail['ItemRef']['value'],'90001')
    def test_existing_receipt_different_item_or_money_cannot_skip(self):
        p=dict(DocNumber='SR',TxnDate='2026-10-01',Line=[dict(DetailType='SalesItemLineDetail',Amount=100,SalesItemLineDetail={'ItemRef':{'value':'1'},'Qty':1})])
        assert_receipt_matches(p,p)
        for field,value in [('Amount',90),('SalesItemLineDetail',{'ItemRef':{'value':'2'},'Qty':1})]:
            changed={**p,'Line':[{**p['Line'][0],field:value}]}
            with self.assertRaises(ValueError):assert_receipt_matches(p,changed)
    def test_existing_receipt_qbo_default_deposit_is_not_a_mismatch(self):
        p=dict(DocNumber='SR',TxnDate='2026-10-01',Line=[dict(DetailType='SalesItemLineDetail',Amount=100,SalesItemLineDetail={'ItemRef':{'value':'1'},'Qty':1})])
        posted={**p,'DepositToAccountRef':{'value':'72'}}
        assert_receipt_matches(p,posted)
        with self.assertRaises(ValueError):
            assert_receipt_matches({**p,'DepositToAccountRef':{'value':'35'}},posted)
    def test_reconcile_failure_holds_future_posts_no_automatic_clear(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('code_scripts.operations_controls.posting_hold_path',return_value=Path(tmp)/'hold.json'):
            require_reconciliation_match('company_b',{'status':'MISMATCH'});assert_no_posting_hold()
            with self.assertRaises(RuntimeError):require_reconciliation_match('company_a',{'status':'NOT RUN'})
            with self.assertRaises(ValueError):assert_no_posting_hold()
            require_reconciliation_match('company_a',{'status':'MATCH'})
            with self.assertRaises(ValueError):assert_no_posting_hold()
