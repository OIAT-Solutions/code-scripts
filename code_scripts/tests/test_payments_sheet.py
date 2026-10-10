"""Nora Mart Payments workbook <-> QuickBooks (fake sheet and fake QBO, no network)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_scripts.akponora_ops import payments_sheet as ps

ACCT = "Moniepoint 4686987227 - 100202"


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, json.dumps(body)

    def json(self):
        return self._b


class FakeQBO:
    def __init__(self):
        self.invoices = {"80642": {"Id": "80642", "DocNumber": "CR261005-389764", "TxnDate": "2026-10-05", "TotalAmt": 58200,
                                   "Balance": 58200, "CustomerRef": {"value": "334", "name": "Mrs VERA AKPOREHA"}}}
        self.bills = {"80664": {"Id": "80664", "DocNumber": "EPOS-PO-3976", "TxnDate": "2026-10-03", "TotalAmt": 72000,
                                "Balance": 72000, "VendorRef": {"value": "34", "name": "UNCLE SAM'S BAKERY & CAFE"}}}
        self.banks = [{"Id": "1150040002", "Name": "100202 - MONIEPOINT 4686987227"}, {"Id": "29", "Name": "100100 - Petty Cash"}]
        self.posts, self.n = [], 900

    def query_all(self, sql, entity):
        return {"Account": self.banks, "Invoice": list(self.invoices.values()),
                "Bill": [b for b in self.bills.values() if b["Balance"] > 0]}[entity]

    def get_json(self, path, params=None):
        if path == "/preferences":
            return {"Preferences": {"AccountingInfoPrefs": {"BookCloseDate": "2026-09-30"}}}
        kind, i = path.strip("/").split("/")
        return {"Invoice": dict(self.invoices[i])} if kind == "invoice" else {"Bill": dict(self.bills[i])}

    def post_json(self, path, body, requestid):
        self.posts.append((path, body, requestid))
        self.n += 1
        ln = body["Line"][0]
        store = self.invoices if path == "/payment" else self.bills
        t = store[ln["LinkedTxn"][0]["TxnId"]]
        t["Balance"] = round(float(t["Balance"]) - ln["Amount"], 2)
        return Resp(200, {("Payment" if path == "/payment" else "BillPayment"): {"Id": str(self.n)}})


class FakeSheet:
    def __init__(self, credit=(), bills=()):
        self.tabs = {ps.CREDIT_PAYMENTS: [list(ps.CREDIT_PAY_COLS)] + [list(r) for r in credit],
                     ps.BILL_PAYMENTS: [list(ps.BILL_PAY_COLS)] + [list(r) for r in bills],
                     ps.CREDIT_SALES: [list(ps.CREDIT_SALES_COLS)], ps.BILLS: [list(ps.BILLS_COLS)],
                     ps.LISTS: [list(ps.LIST_COLS)]}
        self.bodies, self.cells = {}, {}

    def read(self, tab, cols):
        return [list(r) for r in self.tabs[tab]]

    def replace_body(self, tab, cols, rows, first_col="A"):
        self.bodies[(tab, first_col)] = rows

    def write_cells(self, updates):
        for rng, vals in updates:
            tab, cell = rng.rsplit("!", 1)
            tab, n = tab.strip("'"), int(cell.split(":")[0][1:])
            self.cells[(tab, n)] = vals
            row = self.tabs[tab][n - 1]
            row += [""] * (10 - len(row))
            row[7:10] = vals


def pay(doc, amount, day="06/10/2026", acct=ACCT, ref="", by="Esther"):
    return [doc, amount, day, acct, ref, by, ""]


class PaymentsSheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(ps, "state_dir", lambda tool: self.tmp)
        p.start()
        self.addCleanup(p.stop)
        t = mock.patch.object(ps, "today", lambda: "2026-10-10")
        t.start()
        self.addCleanup(t.stop)
        self.qbo = FakeQBO()
        self.env = {ps.SHEET_ENV: "sheet", ps.POST_ENV: "1"}

    def run_(self, sheet, **kw):
        return ps.run(client=self.qbo, write_client=self.qbo, sheet=sheet, env=kw.pop("env", self.env), **kw)

    def test_part_payments_post_once_into_the_chosen_account(self):
        sheet = FakeSheet(credit=[pay("CR261005-389764", 20000), pay("CR261005-389764", "38,200.00", acct="Cash - 100100 Petty Cash")])
        r = self.run_(sheet)
        self.assertEqual([x["status"] for x in r["credit"]], ["posted", "posted"])
        path, body, rid = self.qbo.posts[0]
        self.assertEqual((path, body["DepositToAccountRef"], body["TxnDate"], body["TotalAmt"]),
                         ("/payment", {"value": "1150040002"}, "2026-10-06", 20000.0))
        self.assertEqual(self.qbo.posts[1][1]["DepositToAccountRef"], {"value": "29"})
        self.assertEqual(self.qbo.invoices["80642"]["Balance"], 0)
        self.assertEqual(sheet.cells[(ps.CREDIT_PAYMENTS, 2)][0], ps.POSTED)
        self.assertEqual(self.run_(sheet)["credit"], [])  # second run: nothing new
        self.assertEqual(len(self.qbo.posts), 2)
        self.assertEqual(sheet.bodies[(ps.CREDIT_SALES, "A")][0][6], "Paid")
        self.assertEqual(sheet.bodies[(ps.LISTS, "B")], [])

    def test_sheet_losing_the_mark_never_posts_twice(self):
        sheet = FakeSheet(credit=[pay("CR261005-389764", 1000)])
        self.run_(sheet)
        sheet.tabs[ps.CREDIT_PAYMENTS][1][7:10] = ["", "", ""]
        self.run_(sheet)
        self.assertEqual(len(self.qbo.posts), 1)
        self.assertEqual(sheet.tabs[ps.CREDIT_PAYMENTS][1][7], ps.POSTED)

    def test_holds(self):
        cases = [(pay("CR999", 100), "is not open"), (pay("CR261005-389764", 99999), "more than"),
                 (pay("CR261005-389764", 100, day="12/10/2026"), "in the future"),
                 (pay("CR261005-389764", 100, day="01/10/2026"), "before the invoice"),
                 (pay("CR261005-389764", 100, acct="Other"), "not one of the listed accounts"),
                 (pay("CR261005-389764", "abc"), "not a positive number")]
        sheet = FakeSheet(credit=[c for c, _ in cases])
        r = self.run_(sheet)
        for (row, why), res in zip(cases, r["credit"]):
            self.assertEqual(res["status"], "held")
            self.assertIn(why, res["reason"])
        self.assertEqual(self.qbo.posts, [])
        self.assertTrue(sheet.tabs[ps.CREDIT_PAYMENTS][1][7].startswith("Held: "))

    def test_bill_payment_and_cap_and_posting_off(self):
        sheet = FakeSheet(bills=[pay("EPOS-PO-3976", 72000, ref="TRF 123")])
        r = self.run_(sheet, env={**self.env, ps.POST_ENV: "0"})
        self.assertEqual(r["bills"][0]["status"], "ready")
        self.assertEqual(self.qbo.posts, [])
        r = self.run_(FakeSheet(bills=[pay("EPOS-PO-3976", 72000)]), env={**self.env, ps.CAP_ENV: "50000"})
        self.assertEqual(r["bills"][0]["status"], "waiting")
        r = self.run_(sheet)
        path, body, _ = self.qbo.posts[0]
        self.assertEqual((path, body["CheckPayment"]["BankAccountRef"], body["DocNumber"], body["VendorRef"]),
                         ("/billpayment", {"value": "1150040002"}, "TRF 123", {"value": "34"}))
        self.assertEqual(self.qbo.bills["80664"]["Balance"], 0)

    def test_wrong_headers_stop_before_anything_posts(self):
        sheet = FakeSheet(credit=[pay("CR261005-389764", 1000)])
        sheet.tabs[ps.CREDIT_PAYMENTS][0][1] = "Amount"
        with self.assertRaises(ps.SheetError):
            self.run_(sheet)
        self.assertEqual(self.qbo.posts, [])

    def test_off_without_sheet_id(self):
        self.assertFalse(ps.run(client=self.qbo, sheet=FakeSheet(), env={})["enabled"])

    def test_sheet_dates(self):
        self.assertEqual(ps.parse_day("06/10/2026"), "2026-10-06")
        self.assertEqual(ps.parse_day(46301), "2026-10-06")
        self.assertEqual(ps.parse_day("6 Oct"), "")


if __name__ == "__main__":
    unittest.main()


class DailyRunPaymentsStepTests(unittest.TestCase):
    def test_off_without_sheet_and_held_rows_go_to_review(self):
        from code_scripts.akponora_ops import daily_run as dr
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(ps, "state_dir", lambda tool: Path(folder)):
            run = dr.DailyRun("2026-10-09", root=Path(folder), runner=lambda *a, **k: 0, env={})
            res = dr.StepResult(name="payments", out=str(run.step_dir("payments")))
            run.step_payments(res)
            self.assertEqual(res.status, dr.DISABLED)
            fake = {"post": True, "credit": [{"row": 2, "doc": "CR1", "amount": "100", "status": "held", "reason": "x"}],
                    "bills": [{"row": 2, "doc": "EPOS-PO-1", "amount": "50", "status": "posted"}], "open_credit": 1, "open_bills": 3}
            run = dr.DailyRun("2026-10-09", root=Path(folder), runner=lambda *a, **k: 0, env={ps.SHEET_ENV: "s", ps.POST_ENV: "1"},
                              payments_client=object(), payments_write_client=object())
            res = dr.StepResult(name="payments", out=str(run.step_dir("payments")))
            with mock.patch.object(ps, "run", return_value=fake):
                run.step_payments(res)
            self.assertEqual(res.status, dr.REVIEW)
            self.assertIn("credit payment row 2 CR1", res.review[0])
            self.assertIn("bill payments 1", dr.step_line(res.__dict__))
