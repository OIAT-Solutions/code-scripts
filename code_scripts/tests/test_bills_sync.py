"""bills_sync: EPOS PO receipts -> QBO Bill plan / post. All QBO HTTP is faked; no EPOS, no network."""
import argparse
import csv
import json
import os
import re
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

from code_scripts.akponora_ops import bills_sync as bs
from code_scripts.akponora_ops.common import MAPPING_COLUMNS
from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient, StopRun

APPROVER = "Owner (test)"


def write_csv(path, cols, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def map_row(pid, name, typ, target, sku, item_id, mult="1", family="", unit="Each"):
    return {"Row ID": f"EPOS-{pid}", "EPOS Product ID": pid, "EPOS Existing SKU": "", "EPOS Name": name,
            "Pipeline Status": "TIER_A", "Review Status": "Approved", "Target QBO Item Type": typ,
            "Target QBO Name": target, "Target QBO SKU": sku, "Target QBO Item Id": item_id,
            "Staff Approved Sale Multiplier": mult, "Effective Date": "2026-10-01", "Approved By": APPROVER,
            "Canonical Family Key": family if typ == "Inventory" else "", "Canonical Unit": unit if typ == "Inventory" else "",
            "Staff Approved Purchase Multiplier": mult}


ITEMS = {
    "5001": {"Id": "5001", "Name": "COKE CAN", "Sku": "AKP-100", "Type": "Inventory", "Active": True,
             "QtyOnHand": -10, "PurchaseCost": 200, "AssetAccountRef": {"value": "77", "name": "Inventory Asset"},
             "ExpenseAccountRef": {"value": "1150040034", "name": "200202"}},
    "5002": {"Id": "5002", "Name": "WATER 75CL", "Sku": "AKP-200", "Type": "Inventory", "Active": True,
             "QtyOnHand": 3, "PurchaseCost": 100, "AssetAccountRef": {"value": "77", "name": "Inventory Asset"}},
    "5003": {"Id": "5003", "Name": "EGGS LOOSE", "Sku": "AKP-NS-300", "Type": "NonInventory", "Active": True,
             "PurchaseCost": 150, "ExpenseAccountRef": {"value": "74", "name": "200100 - Purchases - Groceries"}},
    "5004": {"Id": "5004", "Name": "LEGACY \u2014 OLD SOAP", "Sku": "AKP-400", "Type": "Inventory", "Active": True,
             "PurchaseCost": 50, "AssetAccountRef": {"value": "77"}},
    "15030": {"Id": "15030", "Name": "AKP-UNMAPPED-EPOS-SALES", "Sku": "AKP-NS-500", "Type": "NonInventory",
              "Active": True, "ExpenseAccountRef": {"value": "74"}},
}
VENDORS = {"10": {"Id": "10", "DisplayName": "NIGERIAN BOTTLING COMPANY", "Active": True},
           "11": {"Id": "11", "DisplayName": "UNCLE SAMS BAKERY", "Active": True}}


def order(ref, products, *, received="2026-10-02T10:00:00", supplier="NIGERIAN BOTTLING COMPANY",
          payment="TRANSFER", total_inc=None, total_ex=None, status="Received"):
    """products: (pid, name, qty, qty_received, unit_ex, unit_inc)."""
    prods = [{"Id": i, "ProductId": int(pid), "ProductName": name, "CategoryID": 1, "Barcode": None,
              "TaxRatePercentage": 7.5, "ValueIncTax": inc, "ValueExcTax": ex, "ActualProductCostPrice": ex,
              "Quantity": qty, "QuantityReceived": qr, "VolumeOfSale": None, "CostPriceMeasurementUnitVolume": None,
              "Factor": None} for i, (pid, name, qty, qr, ex, inc) in enumerate(products, start=1)]
    inc = sum(Decimal(str(p["QuantityReceived"])) * Decimal(str(p["ValueIncTax"])) for p in prods)
    ex = sum(Decimal(str(p["QuantityReceived"])) * Decimal(str(p["ValueExcTax"])) for p in prods)
    lst = {"OrderRef": ref, "StatusName": status, "Status": 2, "DateReceived": received,
           "TotalValueReceived": float(total_inc if total_inc is not None else inc),
           "TotalValueReceivedExTax": float(total_ex if total_ex is not None else ex),
           "GoodsReceiptNumbers": [f"GRN{ref}"], "SupplierId": None}
    det = {"OrderRef": ref, "StatusName": status, "SupplierId": 0,
           "Note": f"SUPPLIER: {supplier} MODE OF PAYMENT: {payment}", "Products": prods}
    return lst, det


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeQBO:
    def __init__(self, bills=(), items=None, vendors=None, book_close="2026-08-31"):
        self.items = dict(items or ITEMS)
        self.vendors = dict(vendors or VENDORS)
        self.bills = {b["Id"]: dict(b) for b in bills}
        self.book_close = book_close
        self.calls = []
        self.next_id = 9000

    def request(self, method, url, params=None, headers=None, data=None, timeout=None):
        params = params or {}
        path = url.split("/v3/company/x", 1)[-1]
        self.calls.append((method, path, dict(params)))
        if method == "GET" and path == "/query":
            return Resp(200, {"QueryResponse": self._query(params["query"])})
        if method == "GET" and path == "/preferences":
            return Resp(200, {"Preferences": {"AccountingInfoPrefs": {"BookCloseDate": self.book_close}}})
        for prefix, store, key in (("/item/", self.items, "Item"), ("/vendor/", self.vendors, "Vendor"),
                                   ("/bill/", self.bills, "Bill")):
            if method == "GET" and path.startswith(prefix):
                return Resp(200, {key: store[path.rsplit("/", 1)[1]]})
        if method == "POST" and path == "/bill":
            body = json.loads(data)
            self.next_id += 1
            total = round(sum(ln["Amount"] for ln in body["Line"]), 2)
            bill = {**body, "Id": str(self.next_id), "SyncToken": "0", "TotalAmt": total, "Balance": total}
            self.bills[bill["Id"]] = bill
            return Resp(200, {"Bill": bill})
        raise AssertionError(f"unexpected {method} {path}")

    def _query(self, sql):
        page = lambda rows: rows if "startposition" not in sql or int(re.search(r"startposition (\d+)", sql).group(1)) == 1 else []
        if "from Item" in sql:
            ids = re.findall(r"'([^']*)'", sql)
            return {"Item": [self.items[i] for i in ids if i in self.items]}
        if "from Vendor" in sql:
            return {"Vendor": page(list(self.vendors.values()))}
        if "from Term" in sql:
            return {"Term": page([])}
        if "from Bill" in sql:
            bills = list(self.bills.values())
            m = re.search(r"DocNumber in \(([^)]*)\)", sql)
            if m:
                docs = re.findall(r"'([^']*)'", m.group(1))
                return {"Bill": [b for b in bills if b.get("DocNumber") in docs]}
            m = re.search(r"DocNumber = '([^']*)'", sql)
            if m:
                return {"Bill": [b for b in bills if b.get("DocNumber") == m.group(1)]}
            lo, hi = re.findall(r"TxnDate [<>]= '([^']*)'", sql)[:2]
            return {"Bill": page([b for b in bills if lo <= b["TxnDate"] <= hi])}
        raise AssertionError(sql)

    def methods(self):
        return {c[0] for c in self.calls}

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def client(fake, writes=False):
    return QBOClient(fake, lambda: "tok", "https://x/v3/company/x", allow_writes=writes, sleep=lambda s: None)


class Fixture:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp())
        write_csv(self.tmp / "mapping.csv", MAPPING_COLUMNS, [
            map_row("100", "COKE CAN*24", "Inventory", "COKE CAN", "AKP-100", "5001", mult="24", family="AKP-100"),
            map_row("101", "COKE CAN", "Inventory", "COKE CAN", "AKP-100", "5001", family="AKP-100"),
            map_row("200", "WATER 75CL", "Inventory", "WATER 75CL", "AKP-200", "5002", family="AKP-200"),
            map_row("300", "EGGS LOOSE", "NonInventory", "EGGS LOOSE", "AKP-NS-300", "5003"),
            map_row("400", "OLD SOAP", "Inventory", "OLD SOAP", "AKP-400", "5004", family="AKP-400"),
            map_row("500", "MYSTERY", "NonInventory", "AKP-UNMAPPED-EPOS-SALES", "AKP-NS-500", "15030"),
        ])
        write_csv(self.tmp / "vendors.csv", bs.VENDOR_COLS, [
            {"EPOS Supplier Id": "", "EPOS Supplier Name": "NIGERIAN BOTTLING COMPANY", "QBO Vendor Id": "10",
             "QBO Vendor Name": "NIGERIAN BOTTLING COMPANY", "Approved By": APPROVER},
            {"EPOS Supplier Id": "", "EPOS Supplier Name": "UNCLE SAM'S BAKERY", "QBO Vendor Id": "11",
             "QBO Vendor Name": "UNCLE SAMS BAKERY", "Approved By": APPROVER},
        ])
        self.cursor = self.tmp / "cursor.json"
        self.n = 0

    def plan(self, orders, fake=None, *, date_from="2026-10-01", date_to="2026-10-05", **kw):
        self.n += 1
        out = self.tmp / f"plan{self.n}"
        out.mkdir()
        (out / "po_list.json").write_text(json.dumps({"body": {"orders": [o for o, _ in orders]}}))
        (out / "po_details.jsonl").write_text("".join(json.dumps(d) + "\n" for _, d in orders))
        a = argparse.Namespace(out=str(out), mapping=str(self.tmp / "mapping.csv"), vendors=str(self.tmp / "vendors.csv"),
                               po_list=str(out / "po_list.json"), po_details=str(out / "po_details.jsonl"),
                               date_from=date_from, date_to=date_to, tax_mode=kw.get("tax_mode", "gross"),
                               dup_days=14, dup_min_value="50000", no_slack=True)
        fake = fake or FakeQBO()
        with mock.patch.object(bs, "cursor_path", lambda: self.cursor), \
                mock.patch.object(bs, "today_lagos", lambda: datetime(2026, 10, 6, 9, 0)):
            out, summary, entries = bs.run_plan(a, client=client(fake))
        return out, summary, {e["po"]["ref"]: e for e in entries}, fake

    def registry(self):
        return bs.load_registry(self.tmp / "mapping.csv")

    def post(self, out, fake, *, approve=(), approval_ref="chat yes 2026-10-02", sha=None, auto=False, review=True):
        if review:
            rows = bs.read_csv(out / "review.csv")
            for r in rows:
                r["Approve"] = "yes" if r["PO"] in approve else ""
            bs.write_csv(out / "review.csv", rows, bs.REVIEW_COLS)
        sha = sha if sha is not None else json.loads((out / "summary.json").read_text())["payloads_sha256"]
        with mock.patch.object(bs, "cursor_path", lambda: self.cursor):
            return bs.run_post(out, client=client(fake, writes=True), registry=self.registry(), approval_ref=approval_ref,
                               expect_sha=sha, review_path=None if auto else out / "review.csv", auto=auto)


COKE = ("100", "COKE CAN*24", 5, 5, 4800.0, 5160.0)
WATER = ("200", "WATER 75CL", 10, 10, 100.0, 107.5)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def test_multiplier_applied_crate_to_cans(self):
        _, _, e, _ = self.f.plan([order(3970, [COKE])])
        self.assertEqual(e["3970"]["status"], "READY", e["3970"]["reasons"])
        ln = e["3970"]["payload"]["Line"][0]
        d = ln["ItemBasedExpenseLineDetail"]
        self.assertEqual((d["ItemRef"]["value"], d["Qty"], d["UnitPrice"]), ("5001", 120.0, 215.0))  # 5160 / 24
        self.assertEqual(ln["Amount"], 25800.0)
        self.assertEqual(d["TaxCodeRef"], {"value": "7"})
        p = e["3970"]["payload"]
        self.assertEqual((p["DocNumber"], p["TxnDate"], p["DueDate"]), ("EPOS-PO-3970", "2026-10-02", "2026-10-02"))
        self.assertEqual((p["APAccountRef"], p["GlobalTaxCalculation"], p["VendorRef"]["value"]),
                         ({"value": "1150040014"}, "TaxExcluded", "10"))
        self.assertIn("EPOS PO 3970", p["PrivateNote"])
        self.assertIn("created by bills_sync", p["PrivateNote"])

    def test_split_tax_mode_books_ex_tax_with_vat_code(self):
        _, _, e, _ = self.f.plan([order(3970, [COKE])], tax_mode="split")
        d = e["3970"]["payload"]["Line"][0]
        self.assertEqual((d["Amount"], d["ItemBasedExpenseLineDetail"]["UnitPrice"]), (24000.0, 200.0))
        self.assertEqual(d["ItemBasedExpenseLineDetail"]["TaxCodeRef"], {"value": "2"})
        self.assertEqual(e["3970"]["bill_total"], Decimal("25800.00"))

    def test_partial_receipt_prorates_by_quantity_received(self):
        _, _, e, _ = self.f.plan([order(3971, [("200", "WATER 75CL", 10, 4, 100.0, 107.5)])])
        x = e["3971"]
        self.assertEqual(x["status"], "READY", x["reasons"])
        line = x["payload"]["Line"][0]
        self.assertEqual((line["Amount"], line["ItemBasedExpenseLineDetail"]["Qty"]), (430.0, 4.0))
        self.assertTrue(any("partial receipt: 4 of 10" in w for w in x["warnings"]))

    def test_unmapped_product_holds_whole_po_without_dropping_lines(self):
        out, _, e, _ = self.f.plan([order(3972, [COKE, ("999", "NEW THING", 1, 1, 10.0, 10.75)])])
        self.assertEqual(e["3972"]["status"], "HOLD")
        self.assertTrue(any("run catalogue_sync" in r for r in e["3972"]["reasons"]))
        lines = [r for r in bs.read_csv(out / "review_lines.csv") if r["PO"] == "3972"]
        self.assertEqual({r["EPOS Product ID"] for r in lines}, {"100", "999"})
        self.assertEqual((out / "payloads.jsonl").read_text(), "")

    def test_unmapped_vendor_holds(self):
        _, _, e, _ = self.f.plan([order(3973, [COKE], supplier="BRAND NEW SUPPLIER LTD")])
        self.assertEqual(e["3973"]["status"], "HOLD")
        self.assertTrue(any("vendors.csv" in r for r in e["3973"]["reasons"]))

    def test_legacy_and_catch_all_targets_refused(self):
        _, _, e, _ = self.f.plan([order(3974, [("400", "OLD SOAP", 1, 1, 50.0, 53.75)]),
                                  order(3975, [("500", "MYSTERY", 1, 1, 10.0, 10.75)])])
        self.assertEqual(e["3974"]["status"], "HOLD")
        self.assertTrue(any("LEGACY" in r for r in e["3974"]["reasons"]))
        self.assertEqual(e["3975"]["status"], "HOLD")
        self.assertTrue(any("catch-all" in r for r in e["3975"]["reasons"]))

    def test_september_receipts_excluded_by_business_day(self):
        _, s, e, _ = self.f.plan([order(3960, [COKE], received="2026-09-30T20:00:00"),
                                  order(3961, [WATER], received="2026-10-01T04:30:00"),
                                  order(3967, [WATER], received="2026-10-01T05:30:00")],
                                 date_from="2026-09-30")
        self.assertEqual((e["3960"]["status"], e["3961"]["status"], e["3967"]["status"]),
                         ("EXCLUDED", "EXCLUDED", "READY"))
        self.assertIn("210200", e["3961"]["reasons"][0])
        self.assertEqual(s["payload_count"], 1)

    def test_existing_docnumber_skips(self):
        fake = FakeQBO(bills=[{"Id": "77001", "DocNumber": "EPOS-PO-3970", "TxnDate": "2026-10-02", "TotalAmt": 25800.0,
                               "VendorRef": {"value": "10"}, "Line": []}])
        _, _, e, _ = self.f.plan([order(3970, [COKE])], fake)
        self.assertEqual(e["3970"]["status"], "SKIP")
        self.assertIn("77001", e["3970"]["reasons"][0])

    def test_manual_near_duplicate_holds(self):
        fake = FakeQBO(bills=[{"Id": "76999", "DocNumber": "", "TxnDate": "2026-09-30", "TotalAmt": 25700.0,
                               "VendorRef": {"value": "10", "name": "NIGERIAN BOTTLING COMPANY"}, "Line": []}])
        _, _, e, _ = self.f.plan([order(3970, [COKE])], fake)
        self.assertEqual(e["3970"]["status"], "HOLD")
        self.assertTrue(any("possible manual duplicate: Bill 76999" in r for r in e["3970"]["reasons"]))

    def test_manual_bill_with_same_items_holds(self):
        fake = FakeQBO(bills=[{"Id": "76998", "DocNumber": "", "TxnDate": "2026-10-04", "TotalAmt": 999.0,
                               "VendorRef": {"value": "10"}, "Line": [{"DetailType": "ItemBasedExpenseLineDetail",
                               "ItemBasedExpenseLineDetail": {"ItemRef": {"value": "5001"}}}]}])
        _, _, e, _ = self.f.plan([order(3970, [COKE])], fake)
        self.assertEqual(e["3970"]["status"], "HOLD")
        self.assertTrue(any("same items" in r for r in e["3970"]["reasons"]))

    def test_total_mismatch_holds(self):
        _, _, e, _ = self.f.plan([order(3976, [COKE], total_inc=25900.0)])
        self.assertEqual(e["3976"]["status"], "HOLD")
        self.assertTrue(any("!= PO received total" in r for r in e["3976"]["reasons"]))

    def test_duplicate_epos_po_holds_the_later_one(self):
        big = ("100", "COKE CAN*24", 600, 600, 4800.0, 5160.0)
        _, _, e, _ = self.f.plan([order(3980, [big], received="2026-10-02T10:00:00"),
                                  order(3981, [big], received="2026-10-03T11:00:00", supplier="UNCLE SAM'S BAKERY")])
        self.assertEqual(e["3980"]["status"], "READY", e["3980"]["reasons"])
        self.assertEqual(e["3981"]["status"], "HOLD")
        self.assertTrue(any("possible duplicate receipt of EPOS PO 3980" in r for r in e["3981"]["reasons"]))

    def test_small_repeat_waits_unless_the_supplier_is_routine(self):
        """3 Oct 2026: daily bread (PO 3969 repeats 3967) would otherwise wait for a person every day."""
        bread = ("100", "COKE CAN*24", 5, 5, 4800.0, 5160.0)  # small: under the N50,000 hold threshold
        orders = [order(3967, [bread], received="2026-10-01T10:00:00", supplier="UNCLE SAM'S BAKERY"),
                  order(3969, [bread], received="2026-10-02T09:00:00", supplier="UNCLE SAM'S BAKERY")]
        out, summary, e, _ = self.f.plan(orders)
        self.assertEqual(e["3969"]["status"], "READY")
        self.assertTrue(any("possible duplicate" in w for w in e["3969"]["warnings"]))  # waits in auto mode
        self.assertEqual(summary["routine_repeats"], [])
        from code_scripts.akponora_ops import review_exclusions as rx
        path = rx.path_near(self.f.tmp / "vendors.csv")
        # the other EPOS spelling of the same supplier matches too (3967 vs 3969 on 3 Oct 2026)
        rx.add("routine_repeat", "UNCLE'S SAM BAKERY", reason="daily bread", added_by="owner", path=path)
        out, summary, e, _ = self.f.plan(orders)
        self.assertEqual(e["3969"]["status"], "READY")
        self.assertEqual(e["3969"]["warnings"], [])  # posts automatically
        self.assertEqual([r["po"] for r in summary["routine_repeats"]], ["3969"])
        # 6 Oct 2026 (Marvin): a routine supplier's large repeat (the weekly 40-loaf order) is noted, not held
        big = ("100", "COKE CAN*24", 600, 600, 4800.0, 5160.0)
        orders_big = [order(3980, [big], received="2026-10-01T10:00:00", supplier="UNCLE SAM'S BAKERY"),
                      order(3981, [big], received="2026-10-02T10:00:00", supplier="UNCLE SAM'S BAKERY")]
        _, summary, e, _ = self.f.plan(orders_big)
        self.assertEqual(e["3981"]["status"], "READY", e["3981"]["reasons"])
        self.assertIn("3981", [r["po"] for r in summary["routine_repeats"]])
        # ... while any other supplier's large repeat still holds until a person confirms it
        other = [order(3982, [big], received="2026-10-01T10:00:00", supplier="NIGERIAN BOTTLING COMPANY"),
                 order(3983, [big], received="2026-10-02T10:00:00", supplier="NIGERIAN BOTTLING COMPANY")]
        _, _, e, _ = self.f.plan(other)
        self.assertEqual(e["3983"]["status"], "HOLD")
        rx.add("repeat_ok", "3983", reason="confirmed real order", added_by="owner", path=path)
        _, _, e, _ = self.f.plan(other)
        self.assertEqual(e["3983"]["status"], "READY", e["3983"]["reasons"])

    def test_receipt_on_non_master_child_holds(self):
        _, _, e, _ = self.f.plan([order(3977, [("101", "COKE CAN", 24, 24, 200.0, 215.0)])])
        self.assertEqual(e["3977"]["status"], "HOLD")
        self.assertTrue(any("child of AKP-100" in r for r in e["3977"]["reasons"]))

    def test_cost_far_from_purchase_cost_holds(self):
        _, _, e, _ = self.f.plan([order(3978, [("200", "WATER 75CL", 1, 1, 2400.0, 2580.0)])])
        self.assertEqual(e["3978"]["status"], "HOLD")
        self.assertTrue(any("multiplier x1 suspect" in r for r in e["3978"]["reasons"]))

    def test_plan_performs_only_get_requests(self):
        _, _, _, fake = self.f.plan([order(3970, [COKE]), order(3971, [WATER])])
        self.assertEqual(fake.methods(), {"GET"})
        self.assertFalse(fake.posts())

    def test_requestid_and_sha_deterministic(self):
        _, s1, e1, _ = self.f.plan([order(3970, [COKE])])
        _, s2, e2, _ = self.f.plan([order(3970, [COKE])])
        self.assertEqual(s1["payloads_sha256"], s2["payloads_sha256"])
        self.assertEqual(e1["3970"]["requestid"], e2["3970"]["requestid"])
        self.assertEqual(e1["3970"]["requestid"], bs.requestid_for("EPOS-PO-3970", e1["3970"]["payload_sha256"]))
        self.assertNotEqual(bs.requestid_for("EPOS-PO-3970", "x" * 64), e1["3970"]["requestid"])
        self.assertLessEqual(len(bs.doc_number(99999999)), 21)


class PostTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.out, self.summary, self.e, _ = self.f.plan([order(3970, [COKE]), order(3971, [WATER])],
                                                        date_from="2026-10-01", date_to="2026-10-02")
        self.fake = FakeQBO()

    def test_refuses_without_approve_column(self):
        rows = bs.read_csv(self.out / "review.csv")
        bs.write_csv(self.out / "review.csv", rows, [c for c in bs.REVIEW_COLS if c != "Approve"])
        with self.assertRaisesRegex(StopRun, "Approve column"):
            self.f.post(self.out, self.fake, review=False)
        self.assertFalse(self.fake.posts())

    def test_refuses_without_approval_ref_or_sha(self):
        with self.assertRaisesRegex(StopRun, "approval-ref"):
            self.f.post(self.out, self.fake, approve=("3970",), approval_ref="")
        with self.assertRaisesRegex(StopRun, "expect-sha"):
            self.f.post(self.out, self.fake, approve=("3970",), sha="")
        with self.assertRaisesRegex(StopRun, "expect-sha"):
            self.f.post(self.out, self.fake, approve=("3970",), sha="0" * 64)
        self.assertFalse(self.fake.posts())

    def test_posts_only_approved_rows_and_verifies(self):
        res = self.f.post(self.out, self.fake, approve=("3970",))
        self.assertIsNone(res["stopped"])
        self.assertEqual(res["counts"], {"POSTED": 1})
        posts = self.fake.posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["requestid"], self.e["3970"]["requestid"])
        bill = [b for b in self.fake.bills.values() if b["DocNumber"] == "EPOS-PO-3970"][0]
        self.assertEqual(bill["TotalAmt"], 25800.0)
        results = bs.read_csv(self.out / "results.csv")
        self.assertEqual([(r["PO"], r["status"]) for r in results], [("3970", "POSTED")])
        # resumable: a re-run posts nothing new
        again = self.f.post(self.out, self.fake, approve=("3970",))
        self.assertEqual(again["counts"], {"ALREADY_DONE": 1})
        self.assertEqual(len(self.fake.posts()), 1)

    def test_cursor_advances_only_over_done_days(self):
        self.f.post(self.out, self.fake, approve=("3970", "3971"))
        cur = json.loads(self.f.cursor.read_text())
        self.assertEqual(cur["last_complete_business_date"], "2026-10-02")
        f2 = Fixture()
        out, _, _, _ = f2.plan([order(3970, [COKE]), order(3972, [WATER], received="2026-10-03T09:00:00")],
                               date_from="2026-10-01", date_to="2026-10-04")
        f2.post(out, FakeQBO(), approve=("3970",))
        self.assertEqual(json.loads(f2.cursor.read_text())["last_complete_business_date"], "2026-10-02")

    def test_approving_a_hold_is_refused(self):
        out, _, e, _ = self.f.plan([order(3973, [COKE], supplier="BRAND NEW SUPPLIER LTD")])
        with self.assertRaisesRegex(StopRun, "only READY"):
            self.f.post(out, self.fake, approve=("3973",))

    def test_live_duplicate_found_at_post_time_is_held(self):
        self.fake.bills["76000"] = {"Id": "76000", "DocNumber": "", "TxnDate": "2026-10-02", "TotalAmt": 25800.0,
                                    "VendorRef": {"value": "10"}, "Line": []}
        res = self.f.post(self.out, self.fake, approve=("3970",))
        self.assertEqual(res["counts"], {"HELD_LIVE": 1})
        self.assertFalse(self.fake.posts())

    def test_existing_matching_bill_is_adopted_not_reposted(self):
        payload = json.loads((self.out / "payloads.jsonl").read_text().splitlines()[0])["payload"]
        self.fake.bills["76100"] = {**payload, "Id": "76100", "TotalAmt": 25800.0, "Balance": 25800.0}
        res = self.f.post(self.out, self.fake, approve=("3970",))
        self.assertEqual(res["counts"], {"ADOPTED": 1})
        self.assertFalse(self.fake.posts())

    def test_auto_mode_requires_env_and_applies_caps(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(bs.AUTO_ENV, None)
            with self.assertRaisesRegex(StopRun, bs.AUTO_ENV):
                self.f.post(self.out, self.fake, auto=True, review=False)
        env = {bs.AUTO_ENV: "1", bs.AUTO_REF_ENV: "owner standing yes", bs.AUTO_MAX_BILL_ENV: "10000",
               bs.AUTO_MAX_COUNT_ENV: "5"}
        with mock.patch.dict(os.environ, env):
            res = self.f.post(self.out, self.fake, auto=True, review=False)
        self.assertEqual(res["counts"], {"CAPPED": 1, "POSTED": 1})  # 25,800 > cap; water 1,075 posts
        self.assertEqual([c[2]["requestid"] for c in self.fake.posts()], [self.e["3971"]["requestid"]])
        with mock.patch.dict(os.environ, {**env, bs.AUTO_MAX_BILL_ENV: "1000000", bs.AUTO_MAX_COUNT_ENV: "0"}):
            f = Fixture()
            out, _, _, _ = f.plan([order(3990, [WATER])])
            res = f.post(out, FakeQBO(), auto=True, review=False)
        self.assertEqual(res["counts"], {"CAPPED": 1})

    def test_mapping_change_after_plan_holds_bill(self):
        rows = bs.read_csv(self.f.tmp / "mapping.csv")
        for r in rows:
            if r["EPOS Product ID"] == "100":
                r["Staff Approved Sale Multiplier"] = r["Staff Approved Purchase Multiplier"] = "12"
        write_csv(self.f.tmp / "mapping.csv", MAPPING_COLUMNS, rows)
        res = self.f.post(self.out, self.fake, approve=("3970",))
        self.assertEqual(res["counts"], {"HELD_LIVE": 1})
        self.assertFalse(self.fake.posts())


class VendorTests(unittest.TestCase):
    def test_suggestions_prefer_september_evidence_then_fuzzy(self):
        suppliers = {bs.vendor_key(n): {"names": Counter({n: 1}), "refs": refs, "id": ""}
                     for n, refs in (("RITE FOODS", {"1", "2"}), ("UNCLE SAMS BAKERY", {"3"}), ("ZZQ", {"4"}))}
        vendors = {**VENDORS, "12": {"Id": "12", "DisplayName": "RIE FOODS LIMITED", "Active": True}}
        history = {bs.vendor_key("RITE FOODS"): Counter({"12": 3})}
        rows = {r["EPOS Supplier Name"]: r for r in bs.suggest_vendors(suppliers, vendors, history, set(), {})}
        self.assertEqual(rows["RITE FOODS"]["Suggested QBO Vendor Id"], "12")
        self.assertTrue(rows["RITE FOODS"]["Evidence"].startswith("September"))
        self.assertEqual(rows["UNCLE SAMS BAKERY"]["Suggested QBO Vendor Id"], "11")
        self.assertEqual(rows["ZZQ"]["Suggested QBO Vendor Id"], "")
        self.assertTrue(all(r["Approved By"] == "" for r in rows.values()))


if __name__ == "__main__":
    unittest.main()


class PoDetailCacheTests(unittest.TestCase):
    """3 Oct 2026: the bills step re-opened 65 unchanged earlier POs every night (18 of 26 minutes)."""

    def test_only_unchanged_lookback_pos_come_from_the_cache(self):
        import tempfile
        from pathlib import Path

        old = {"OrderRef": 3850, "DateReceived": "2026-09-20T10:00:00", "TotalValueReceived": 60000}
        edited = {"OrderRef": 3851, "DateReceived": "2026-09-21T10:00:00", "TotalValueReceived": 70000}
        today = {"OrderRef": 3970, "DateReceived": "2026-10-02T10:00:00", "TotalValueReceived": 20000}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / bs.PO_CACHE
            rows = [{"ref": "3850", "fingerprint": bs.po_fingerprint(old), "detail": {"OrderRef": 3850}},
                    {"ref": "3851", "fingerprint": bs.po_fingerprint({**edited, "TotalValueReceived": 1}),
                     "detail": {"OrderRef": 3851}},
                    {"ref": "3970", "fingerprint": bs.po_fingerprint(today), "detail": {"OrderRef": 3970}}]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
            cache = bs.load_po_cache(path)
        got = bs.cached_details([old, edited, today], cache, lambda o: o["DateReceived"] < "2026-10-01")
        self.assertEqual(set(got), {"3850"})  # edited row and in-window PO are opened live
        self.assertEqual(bs.load_po_cache(Path("/nonexistent/x.jsonl")), {})

    def test_cache_is_pruned_to_the_current_window(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / bs.PO_CACHE
            rows = [{"ref": str(r), "fingerprint": "f", "detail": {"OrderRef": r}} for r in (3700, 3850, 3850, 3851)]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            self.assertEqual(bs.prune_po_cache(path, {"3850", "3851", "3999"}), 2)
            self.assertEqual(sorted(bs.load_po_cache(path)), ["3850", "3851"])
            self.assertEqual(len(path.read_text().splitlines()), 2)  # duplicates collapsed, old POs dropped
