"""Recurring-order decisions and posting integration; all financial IO is fake."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from code_scripts.akponora_ops import purchase_patterns as pp, bills_sync as bs, vendors as vs
from code_scripts.tests.test_bills_sync import Fixture, FakeQBO, order, StopRun


def policy():
    return {'version': 1, 'company': 'company_a', 'approval_ref': 'Owner test recurring water',
            'max_bill_total': '50000', 'minimum_prior_days': 2, 'lookback_days': 35,
            'products': [{'product_id': '200', 'vendor_ids': ['10', '11'],
                          'max_quantity': '20', 'unit_inc': '107.5'}]}


WATER = ('200', 'WATER 75CL', 10, 10, 100.0, 107.5)


def po(ref, day, **kwargs):
    return bs.build_po(*order(ref, [WATER], received=f'2026-10-{day:02d}T10:00:00', **kwargs))


class PatternTests(unittest.TestCase):
    def setUp(self):
        self.history = [po(1, 1), po(2, 2)]
        self.current = po(3, 4)

    def decide(self, current=None, history=None, rules=None, vendor='10'):
        return pp.expected_repeat(current or self.current, self.history if history is None else history,
                                  vendor_id=vendor, vendor_for=lambda p: p.get('_vendor_id', '10'),
                                  policy=rules or policy())

    def test_earlier_receipt_days_and_references_are_recorded(self):
        result = self.decide()
        self.assertEqual(result['products'][0]['prior_days'], ['2026-10-01', '2026-10-02'])
        self.assertEqual(result['products'][0]['prior_pos'], ['1', '2'])

    def test_future_and_same_day_receipts_cannot_supply_frequency(self):
        self.assertIsNone(self.decide(history=[po(4, 5), po(5, 6)]))
        self.assertIsNone(self.decide(history=[po(4, 1), po(5, 1)]))

    def test_same_day_copy_remains_a_conflict(self):
        self.assertIsNone(self.decide(history=self.history + [po(4, 4)]))

    def test_two_confirmed_sellers_of_a_commodity_can_receive_same_day(self):
        other = po(4, 4)
        other['_vendor_id'] = '11'
        self.assertIsNotNone(self.decide(history=self.history + [other]))

    def test_reused_receipt_number_is_not_an_expected_repeat(self):
        self.current['grn'] = self.history[0]['grn']
        self.assertIsNone(self.decide())

    def test_unknown_supplier_or_unapproved_product_never_inferred(self):
        self.assertIsNone(self.decide(vendor=''))
        self.assertIsNone(self.decide(vendor='12'))
        self.current['lines'][0]['product_id'] = '999'
        self.assertIsNone(self.decide())

    def test_quantity_price_and_receipt_value_bounds(self):
        for field, value in [('qty_received', '21'), ('unit_inc', '150')]:
            with self.subTest(field=field):
                changed = copy.deepcopy(self.current)
                changed['lines'][0][field] = value
                self.assertIsNone(self.decide(current=changed))
        self.current['total_inc'] = '50000.01'
        self.assertIsNone(self.decide())

    def test_missing_or_stale_evidence_stays_unclassified(self):
        self.current['grn'] = ''
        self.assertIsNone(self.decide())
        self.current = po(3, 4)
        self.history[0]['has_detail'] = False
        self.assertIsNone(self.decide())
        self.assertIsNone(self.decide(rules={**policy(), 'lookback_days': 1}))

    def test_policy_needs_owner_approval_and_valid_finite_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.json'
            self.assertIsNone(pp.load_policy(path))
            path.write_text(json.dumps(policy()))
            self.assertEqual(pp.load_policy(path)['approval_ref'], policy()['approval_ref'])
            for change in [{'approval_ref': ''}, {'max_bill_total': 'NaN'}, {'minimum_prior_days': 1}]:
                path.write_text(json.dumps({**policy(), **change}))
                with self.assertRaises(ValueError):
                    pp.load_policy(path)


class PlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.path = self.fixture.tmp / 'purchase_patterns.json'
        self.path.write_text(json.dumps(policy()))
        self.orders = [order(1, [WATER], received='2026-10-01T10:00:00'),
                       order(2, [WATER], received='2026-10-02T10:00:00'),
                       order(3, [WATER], received='2026-10-04T10:00:00')]

    def test_only_duplicate_signal_is_replaced_by_audited_expected_repeat(self):
        out, summary, entries, fake = self.fixture.plan(self.orders)
        self.assertEqual(entries['3']['status'], 'READY')
        self.assertEqual(entries['3']['warnings'], [])
        self.assertTrue(entries['3']['expected_repeat']['products'])
        self.assertEqual(entries['2']['warnings'][0].split(':')[0],
                         'possible duplicate receipt of EPOS PO 1 (2026-10-01, NIGERIAN BOTTLING COMPANY, 1075.00)')
        result = self.fixture.post(out, fake, approve=['3'])
        self.assertEqual(result['counts']['POSTED'], 1)
        self.assertEqual(len(fake.posts()), 1)
        _, _, again, _ = self.fixture.plan(self.orders, fake=fake)
        self.assertEqual(again['3']['status'], 'SKIP')

    def test_changed_or_removed_policy_invalidates_the_prepared_batch(self):
        out, _, _, fake = self.fixture.plan(self.orders)
        self.path.unlink()
        with self.assertRaisesRegex(StopRun, 'purchase-pattern approval changed'):
            self.fixture.post(out, fake, approve=['3'])
        self.assertEqual(fake.posts(), [])

    def test_manual_bill_duplicate_still_blocks_a_familiar_order(self):
        existing = {'Id': '8999', 'DocNumber': 'MANUAL-WATER', 'TxnDate': '2026-10-04',
                    'TotalAmt': 1075, 'VendorRef': {'value': '10'},
                    'Line': [{'ItemBasedExpenseLineDetail': {'ItemRef': {'value': '5002'}}}]}
        _, _, entries, _ = self.fixture.plan(self.orders, fake=FakeQBO(bills=[existing]))
        self.assertEqual(entries['3']['status'], 'HOLD')
        self.assertTrue(any('manual duplicate' in r for r in entries['3']['reasons']))


class SupplierReaderTests(unittest.TestCase):
    def test_observed_supplier_label_and_payment_delimiter(self):
        for note, name, pay in [
            ('SUPPLER: JPO INTEGRATED SERVICES LIMITED, MODE OF PAYMENT: TRANSFER',
             'JPO INTEGRATED SERVICES LIMITED', 'TRANSFER'),
            ('SUPPLER: FLOURISH WATER, MODE OF PAYMENT: CASH', 'FLOURISH WATER', 'CASH'),
            ('SUPPLIER: LEOBERT ENTERPRISES, MODE OF TRANSFER: TRANSFER', 'LEOBERT ENTERPRISES', 'TRANSFER')]:
            self.assertEqual(bs.parse_po_note(note)[:2], (name, pay))
        self.assertEqual(bs.parse_po_note('MODE OF PAYMENT: CASH')[0], '')

    def test_short_name_does_not_match_unrelated_businesses(self):
        unrelated = {'Id': '34', 'DisplayName': "UNCLE SAM'S BAKERY & CAFE", 'Active': True}
        self.assertEqual(vs.score('A+ COMPANY', unrelated), 0)
        result = vs.plan_actions([{'name': 'A+ COMPANY', 'key': 'A', 'po_refs': ['1']}], {'34': unrelated})[0]
        self.assertEqual(result['state'], vs.HOLD_NEAR)
        self.assertEqual(bs.vendor_key('A+ COMPANY'), 'A+')
        self.assertNotEqual(bs.vendor_key('A+ COMPANY'), bs.vendor_key('A COMPANY'))

    def test_brand_is_not_automatically_linked_to_manufacturer(self):
        manufacturer = {'Id': '59', 'DisplayName': 'CWAY', 'Active': True}
        result = vs.plan_actions([{'name': 'CWAY', 'key': 'CWAY', 'po_refs': ['1']}], {'59': manufacturer})[0]
        self.assertEqual(result['state'], vs.HOLD_NEAR)
        self.assertIn('actual seller', result['detail'])


if __name__ == '__main__':
    unittest.main()
