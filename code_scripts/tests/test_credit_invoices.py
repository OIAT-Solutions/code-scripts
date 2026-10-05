"""EPOS credit sales: split out of the sales receipts, then one QBO Invoice per customer per day."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from code_scripts import credit_sales
from code_scripts.akponora_ops import credit_invoices as ci

COLS = ["Staff", "Customer Full Name", "Location Name", "Quantity", "Product", "Date/Time", "TOTAL Sales", "Tender", "Customer ID"]
ROWS = [
    ["ERNEST", "", "NORA", "1", "BREAD", "05/10/2026 10:00:00", "600.00000", "Cash", ""],
    ["ERNEST", "Mrs VERA AKPOREHA ", "NORA", "2", "MALTA", "05/10/2026 18:18:57", "27000.00000", "Credit", "389764"],
    ["ERNEST", "Mrs VERA AKPOREHA ", "NORA", "2", "BREAD", "05/10/2026 18:18:57", "1200.00000", "Credit", "389764"],
    ["ERNEST", "Mr Precious Akporeha ", "NORA", "1", "BREAD", "05/10/2026 15:55:24", "600.00000", "Credit", "390601"],
    ["ERNEST", "GOLDPLATE RESTAURANT - TALEA MALL", "NORA", "1", "RICE", "05/10/2026 12:00:00", "50000.00000", "Credit", "111"],
    ["ERNEST", "GOLDPLATE RESTAURANT - DREAM PARK", "NORA", "1", "OIL", "05/10/2026 13:00:00", "20000.00000", "Credit", "222"],
    ["ERNEST", "", "NORA", "1", "WATER", "05/10/2026 14:00:00", "300.00000", "Cash/Credit", ""],
    ["Total:", "", "", "", "", "", "99700.00000", "", ""],
]


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


class FakeQBO:
    def __init__(self, customers):
        self.customers, self.invoices, self.posts = list(customers), [], []

    def query_all(self, sql, entity):
        if entity == "Customer":
            return list(self.customers)
        doc = sql.split("DocNumber = '")[1].split("'")[0]
        return [i for i in self.invoices if i["DocNumber"] == doc]

    def post_json(self, path, body, requestid):
        self.posts.append((path, body))
        if path == "/customer":
            c = {"Id": str(900 + len(self.customers)), "DisplayName": body["DisplayName"], "Active": True}
            self.customers.append(c)
            return FakeResp({"Customer": c})
        total = sum(Decimal(str(l["SalesItemLineDetail"]["TaxInclusiveAmt"])) for l in body["Line"])
        inv = {"Id": str(5000 + len(self.invoices)), "DocNumber": body["DocNumber"], "TotalAmt": float(total)}
        self.invoices.append(inv)
        return FakeResp({"Invoice": inv})


def fake_lines(rows, day, config, registry):
    total = sum(ci.money(v) for v in rows["TOTAL Sales"])
    return [{"Amount": float(total), "SalesItemLineDetail": {"TaxInclusiveAmt": float(total)}}], total


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="oiat_credit_", dir=os.getenv("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.raw = self.root / "BookKeeping_2026-10-05.csv"
        pd.DataFrame(ROWS, columns=COLS).to_csv(self.raw, index=False)
        p = mock.patch.object(ci, "invoice_lines", side_effect=fake_lines)
        p.start()
        self.addCleanup(p.stop)
        self.config = SimpleNamespace(tax_rate=0.075, tax_code_id="2", get_qbo_config=lambda: {})

    def run_ci(self, client, *, post=True, day="2026-10-05", env=None):
        out = self.root / "out"
        out.mkdir(exist_ok=True)
        env = {ci.MODE_ENV: "post" if post else ""} | (env or {})
        return ci.run(out, business_day=day, client=client, write_client=client if post else None, env=env,
                      config=self.config, registry=object(), state_root=self.root)


class SplitTests(Base):
    def test_credit_rows_leave_the_sales_receipts_and_are_saved(self):
        path, stats = credit_sales.split_raw(str(self.raw), "2026-10-05", state_root=self.root)
        rest = pd.read_csv(path, dtype=str, keep_default_na=False)
        self.assertEqual(list(rest["Tender"]), ["Cash", ""])  # the cash sale + EPOS total row
        self.assertEqual((stats["credit_rows"], stats["credit_total"]), (5, 98800.0))
        self.assertEqual((stats["mixed_rows"], stats["mixed_total"]), (1, 300.0))
        folder = credit_sales.day_dir("2026-10-05", self.root)
        self.assertEqual(len(pd.read_csv(folder / "credit_raw.csv")), 5)
        self.assertTrue((folder / "mixed_raw.csv").exists())

    def test_a_day_without_credit_keeps_the_file_and_clears_stale_rows(self):
        credit_sales.split_raw(str(self.raw), "2026-10-05", state_root=self.root)
        pd.DataFrame([ROWS[0]], columns=COLS).to_csv(self.raw, index=False)
        path, stats = credit_sales.split_raw(str(self.raw), "2026-10-05", state_root=self.root)
        self.assertEqual(path, str(self.raw))
        self.assertFalse((credit_sales.day_dir("2026-10-05", self.root) / "credit_raw.csv").exists())

    def test_only_company_a(self):
        self.assertTrue(credit_sales.enabled("company_a", env={}))
        self.assertFalse(credit_sales.enabled("company_b", env={}))
        self.assertFalse(credit_sales.enabled("company_a", env={credit_sales.ENABLED_ENV: "0"}))


class InvoiceTests(Base):
    def setUp(self):
        super().setUp()
        credit_sales.split_raw(str(self.raw), "2026-10-05", state_root=self.root)
        self.qbo = FakeQBO([{"Id": "62", "DisplayName": "GPFH", "Active": True},
                            {"Id": "311", "DisplayName": "JIFA FELIX", "Active": True}])

    def test_one_invoice_per_customer_goldplates_combined_new_customers_created(self):
        r = self.run_ci(self.qbo)
        by_doc = {i["doc"]: i for i in r["invoices"]}
        self.assertEqual(set(by_doc), {"CR261005-389764", "CR261005-390601", "CR261005-GPFH"})
        self.assertTrue(all(i["status"] == ci.POSTED for i in r["invoices"]))
        self.assertEqual(by_doc["CR261005-GPFH"]["total"], "70000.00")
        gp = next(b for p, b in self.qbo.posts if p == "/invoice" and b["DocNumber"] == "CR261005-GPFH")
        self.assertEqual(gp["CustomerRef"], {"value": "62"})
        self.assertEqual(sorted(c["name"] for c in r["customers_created"]), ["Mr Precious Akporeha", "Mrs VERA AKPOREHA"])
        mapping = ci.load_mapping(ci.mapping_path(self.root))
        self.assertEqual(set(mapping), {"389764", "390601"})
        self.assertEqual(r["mixed"][0]["rows"], 1)

    def test_rerun_never_posts_twice_and_reuses_customers(self):
        self.run_ci(self.qbo)
        posts = len(self.qbo.posts)
        r = self.run_ci(self.qbo)
        self.assertEqual(len(self.qbo.posts), posts)
        self.assertTrue(all(i["status"] == ci.EXISTS for i in r["invoices"]))

    def test_plan_mode_posts_nothing(self):
        r = self.run_ci(self.qbo, post=False)
        self.assertEqual(self.qbo.posts, [])
        self.assertTrue(all(i["status"] == ci.PLANNED for i in r["invoices"]))
        self.assertEqual(r["mode"], "plan")

    def test_cap_and_ambiguous_customer_hold(self):
        self.qbo.customers += [{"Id": "1", "DisplayName": "Vera Akporeha", "Active": True},
                               {"Id": "2", "DisplayName": "VERA AKPOREHA", "Active": True}]
        r = self.run_ci(self.qbo, env={ci.MAX_ENV: "60000"})
        by_doc = {i["doc"]: i for i in r["invoices"]}
        self.assertEqual(by_doc["CR261005-389764"]["status"], ci.HELD)  # two QBO customers match
        self.assertEqual(by_doc["CR261005-GPFH"]["status"], ci.HELD)    # N70,000 over the N60,000 cap
        self.assertEqual(by_doc["CR261005-390601"]["status"], ci.POSTED)

    def test_existing_customer_matched_by_name_ignoring_title(self):
        self.assertEqual(ci.resolve_customer("390601", "Mr Precious Akporeha ",
                                             [{"Id": "7", "DisplayName": "PRECIOUS AKPOREHA", "Active": True}], {})["id"], "7")
        self.assertEqual(ci.customer_key({"Customer Full Name": "GOLDPLATE RESTAURANT – AYANGBUREN", "Customer ID": "9"}),
                         "GPFH")


class SlackTests(unittest.TestCase):
    def test_credit_section_and_todos(self):
        from code_scripts.akponora_ops import daily_run

        steps = [{"name": "credit", "status": "review", "counts": {
            "invoices": [{"status": "posted", "total": "58200.00", "customer": "Mrs VERA AKPOREHA", "customer_key": "389764",
                          "epos_names": ["Mrs VERA AKPOREHA"]},
                         {"status": "held", "total": "70000.00", "epos_names": ["GOLDPLATE RESTAURANT - TALEA MALL"]}],
            "mixed": [], "customers_created": [{"epos_id": "389764", "name": "Mrs VERA AKPOREHA"}]}}]
        text = daily_run.slack_text({"business_date": "2026-10-05", "dry_run": False, "steps": steps, "links": {},
                                     "run_dir": "/x", "status": "review", "finished_at": "2026-10-05T17:10:00+00:00"})
        self.assertIn("*Credit:* ₦58,200 invoiced (1 customer)", text)
        self.assertIn("• Mrs VERA AKPOREHA · ₦58,200 (new customer)", text)
        self.assertIn("GOLDPLATE RESTAURANT - TALEA MALL (₦70,000) is on hold", text)


if __name__ == "__main__":
    unittest.main()
