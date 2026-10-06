import copy
import unittest

from code_scripts.akponora_ops.stock_reconciliation import count_plan, investigate
from code_scripts.akponora_ops.stock_movements import normalise, collect, read_capture
from tempfile import TemporaryDirectory
from pathlib import Path
import json


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.row = {"family_sku": "AKP-1", "qbo_item_id": "101", "qbo_name": "Product", "type": "Inventory",
            "qbo_active": True, "flags": [], "qbo_qty_on_hand": -2, "epos_qty_canonical": 19,
            "difference": 21, "status": "NEGATIVE_QBO"}
        self.snapshot = {"company": "company_a", "rows": [self.row]}
        self.opening = [{"Target QBO SKU": "AKP-1", "QBO Item Id": "101", "QtyOnHand": "0"}]
        self.exclusion = [{"QBO SKU": "AKP-1", "Qty excluded (canonical units)": "21"}]
        self.decision = {"sku": "AKP-1", "cutoff": "2026-10-05", "verified_qty": "21", "qbo_qty_at_cutoff": "0",
            "confirmed_by": "counter", "count_source": "count sheet 1", "reason": "confirmed count",
            "transactions_reconciled_by": "reviewer"}

    def plan(self, decisions=None):
        return count_plan(self.snapshot, decisions if decisions is not None else [self.decision],
            cutoff="2026-10-05", offset_account_id="99", approval_basis="Accountant count variance policy")

    def test_exclusion_is_evidence_not_authorisation(self):
        r = investigate(self.snapshot, self.opening, self.exclusion)
        self.assertEqual(r["summary"]["GAP_EQUALS_EXCLUDED_OPENING"], 1)
        self.assertFalse(r["financial_writes"])
        self.assertIn("deliberately left out", r["rows"][0]["accounting_notes"][0])

    def test_pending_purchase_kept_alongside_opening_and_timing(self):
        self.row["likely_timing"] = True
        r = investigate(self.snapshot, self.opening, self.exclusion,
            [{"PO": "5", "Status": "HOLD"}], [{"PO": "5", "QBO Item Id": "101", "QBO Qty": "55"}])
        clues = r["rows"][0]["clues"]
        self.assertIn("UNPOSTED_PURCHASE_CANDIDATE", clues)
        self.assertIn("TIMING_HINT_NOT_QUANTITY_PROOF", clues)
        self.assertIn("GAP_EQUALS_EXCLUDED_OPENING", clues)

    def test_posted_purchase_is_not_pending(self):
        r = investigate(self.snapshot, self.opening, [], [{"PO": "5", "Status": "ALREADY_POSTED"}],
            [{"PO": "5", "QBO Item Id": "101", "QBO Qty": "55"}])
        self.assertNotIn("UNPOSTED_PURCHASE_CANDIDATE", r["rows"][0]["clues"])

    def test_movement_match_uses_posted_day_and_keeps_incomplete_capture_warning(self):
        self.snapshot["business_context"] = {"last_posted_sales_date": "2026-10-05"}
        events = [{"sku": "AKP-1", "canonical_qty_change": "21", "occurred_at_epos": "05/10/2026 09:00:00",
                   "transfer_id": "42", "holds": []},
                  {"sku": "AKP-1", "canonical_qty_change": "5", "occurred_at_epos": "06/10/2026 09:00:00",
                   "transfer_id": "43", "holds": []}]
        r = investigate(self.snapshot, self.opening, [], movements={"events": events, "errors": ["missing lines"]})
        self.assertEqual(r["rows"][0]["epos_adjustment_qty_through_posted_day"], "21")
        self.assertEqual(r["rows"][0]["epos_adjustment_refs"], ["42"])
        self.assertEqual(r["movement_capture_errors"], ["missing lines"])

    def test_matching_negative_quantities_still_need_count(self):
        self.row.update(epos_qty_canonical=-2, difference=0)
        r = investigate(self.snapshot, self.opening, [])
        self.assertIn("BOTH_SYSTEMS_AGREE_BUT_NEGATIVE", r["rows"][0]["clues"])

    def test_reject_duplicate_opening_identity(self):
        with self.assertRaises(ValueError):
            investigate(self.snapshot, self.opening * 2, [])

    def test_count_plan_is_nonpostable_and_uses_cutoff_not_live_qty(self):
        p = self.plan()
        self.assertFalse(p["postable"])
        self.assertFalse(p["financial_writes"])
        self.assertEqual(p["entries"][0]["quantity_change"], "21")
        changed = copy.deepcopy(self.decision)
        changed["verified_qty"] = "22"
        self.assertNotEqual(p["plan_sha256"], self.plan([changed])["plan_sha256"])

    def test_live_epos_snapshot_alone_cannot_make_plan(self):
        for key in ("confirmed_by", "count_source", "transactions_reconciled_by", "cutoff"):
            d = dict(self.decision)
            d.pop(key)
            with self.assertRaises(ValueError, msg=key):
                self.plan([d])

    def test_blocks_invalid_quantities_and_duplicate_targets(self):
        for quantity in ("NaN", "Infinity", "-1", "wrong"):
            with self.assertRaises(ValueError, msg=quantity):
                self.plan([{**self.decision, "verified_qty": quantity}])
        with self.assertRaises(ValueError):
            self.plan([self.decision, self.decision])

    def test_blocks_legacy_noninventory_inactive_missing_or_flagged_targets(self):
        for change in ({"family_sku": "LEGACY-1"}, {"type": "NonInventory"}, {"qbo_active": False},
                       {"qbo_item_id": "15030"}, {"flags": ["AMBIGUOUS_EPOS_NAME"]}):
            with self.subTest(change=change):
                self.snapshot["rows"] = [{**self.row, **change}]
                with self.assertRaises(ValueError):
                    self.plan()


class MovementTests(unittest.TestCase):
    def test_live_reader_is_available_in_active_package(self):
        from code_scripts.akponora_ops.epos_adjustments import scrape_adjustments
        self.assertTrue(callable(scrape_adjustments))

    def setUp(self):
        self.catalogue = {"1": {"Name": "Master", "IsStockTracked": True, "VolumeOfSale": 12}}
        self.mapping = [{"EPOS Product ID": "1", "Review Status": "Approved", "Target QBO SKU": "AKP-1",
            "Target QBO Item Id": "101", "Target QBO Item Type": "Inventory"}]
        self.capture = {"url": "https://www.eposnowhq.com/Pages/BackOffice/StockAdjustmentDetails.aspx?TransferID=42",
            "info": [["Date", "02/10/2026 09:00:00"], ["Status", "Received"], ["Staff", "Counter"]],
            "items": [["Product", "Product Cost Price\n(Exc. Tax)", "Quantity\nVariance", "Volume\nVariance",
                "Cost Price\nVariance", "Item Reason"], ["Master", "100", "+2", "3", "2300", "Stock Take"]]}

    def run_capture(self, captures=None):
        return normalise(captures if captures is not None else [self.capture], self.catalogue, self.mapping,
            from_date="2026-10-01", through_date="2026-10-05")

    def test_pack_units_and_duplicate_logs(self):
        r = self.run_capture([self.capture, {**self.capture, "_page": 99, "_row": 8}])
        self.assertEqual(len(r["events"]), 1)
        self.assertEqual(r["events"][0]["canonical_qty_change"], "27")
        self.assertEqual(r["events"][0]["po_link_status"], "UNVERIFIED")

    def test_conflicting_transfer_fails_capture(self):
        changed = copy.deepcopy(self.capture)
        changed["items"][1][2] = "+3"
        self.assertTrue(self.run_capture([self.capture, changed])["errors"])

    def test_child_or_ambiguous_name_not_converted(self):
        for case in ("child", "ambiguous"):
            with self.subTest(case=case):
                if case == "child":
                    self.catalogue["1"]["IsStockTracked"] = False
                else:
                    self.catalogue["2"] = dict(self.catalogue["1"])
                event = self.run_capture()["events"][0]
                self.assertIsNone(event["canonical_qty_change"])
                self.assertTrue(event["holds"])

    def test_missing_transfer_or_columns_report_error(self):
        for change in ({"url": "wrong"}, {"items": [["wrong columns"]]}):
            self.assertTrue(self.run_capture([{**self.capture, **change}])["errors"])

    def test_drafts_and_outside_window_not_counted(self):
        for value in (["Status", "Draft"], ["Date", "30/09/2026 09:00:00"]):
            c = copy.deepcopy(self.capture)
            c["info"] = [r for r in c["info"] if r[0] != value[0]] + [value]
            self.assertEqual(self.run_capture([c])["events"], [])

    def test_capture_is_fresh_and_bounded(self):
        called = []
        with TemporaryDirectory() as folder:
            def reader(path, start, sources, company):
                called.append((path, start, sources, company))
            a = collect(Path(folder), "2026-10-01", "2026-10-06", reader=reader)
            b = collect(Path(folder), "2026-10-01", "2026-10-06", reader=reader)
            self.assertNotEqual(a, b)
            self.assertEqual(called[0][2:], ("both", "company_a"))
            with self.assertRaises(ValueError):
                collect(Path(folder), "2026-10-01", "2026-10-31", reader=reader)

    def test_capture_missing_details_is_not_clean(self):
        with TemporaryDirectory() as folder:
            p = Path(folder)
            (p / "epos_stocktakes_list.json").write_text(json.dumps([{"page": 1, "row": 0}]))
            details, errors = read_capture(p)
            self.assertEqual(details, [])
            self.assertEqual(len(errors), 2)  # missing detail and entirely missing movement source


if __name__ == "__main__":
    unittest.main()
