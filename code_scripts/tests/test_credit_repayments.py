import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from code_scripts.akponora_ops import credit_repayments as cr

# Shapes copied from the live EPOS back office, 26 Oct 2026 (read-only)
CREDIT_GRID = [
    ["CUSTOMER", "LOCATION", "DATE/TIME", "AMOUNT"],
    ["Precious Akporeha", "Plot C, Golf Road", "05/10/2026 15:59:29", "₦1,000.00"],
    ["VERA AKPOREHA", "Plot C, Golf Road", "09/10/2026 14:19:15", "₦534,800.00"],
    ["VERA AKPOREHA", "Plot C, Golf Road", "15/10/2026 20:04:53", "₦2,750.00"],
    ["Total:", "", "", "₦539,600.00"],
]
TXN_ROWS = [
    ["STAFF", "LOCATION", "DEVICE", "TABLE/TAB NAME", "DATE/TIME", "CUSTOMER & TYPE", "DISCOUNT", "DISCOUNT REASON",
     "TOTAL", "TENDER", "CHANGE", "", ""],
    ["CELINA HERRICK", "Plot C, Golf Road", "Till2", "", "09/10/2026 19:23:14", "", "-₦10,500.00", "Basket Discount",
     "₦715,300.00", "Transfer", "₦0.00", "", ""],
    ["ERNEST JAITTO", "Plot C, Golf Road", "Till2", "", "09/10/2026 14:19:15", "VERA AKPOREHA (No Type)", "", "",
     "₦534,800.00", "Transfer", "₦0.00", "", ""],
]
CUSTOMER_GRID = [
    ["TITLE", "FIRST NAME", "LAST NAME", "CURRENT BALANCE", "MAX CREDIT", "LAST TRANSACTION DATE"],
    ["Mrs", "VERA", "AKPOREHA", "-₦623,149.99", "₦5,000,000.00", "25/10/2026 19:02:24"],
    ["", "", "", "-₦622,749.99", "", ""],
]
CUSTOMERS = [{"Id": "334", "DisplayName": "Mrs VERA AKPOREHA", "Active": True},
             {"Id": "335", "DisplayName": "Mr Precious Akporeha", "Active": True}]
INVOICES = [{"Id": "80642", "DocNumber": "CR261005-389764", "TxnDate": "2026-10-05", "Balance": 58200,
             "CustomerRef": {"value": "334"}},
            {"Id": "80700", "DocNumber": "CR261014-389764", "TxnDate": "2026-10-14", "Balance": 206300,
             "CustomerRef": {"value": "334"}}]


def qbo(payments=()):
    return {"customers": CUSTOMERS, "invoices": [dict(i) for i in INVOICES], "payments": list(payments), "book_close": ""}


def pay(when, amount, tender="Transfer", customer="VERA AKPOREHA"):
    return {"customer": customer, "location": "Plot C, Golf Road", "when": when, "amount": amount,
            "tender": tender, "staff": "ERNEST JAITTO", "device": "Till2"}


class ParseTests(unittest.TestCase):
    def test_credit_grid_skips_header_and_total(self):
        rows = cr.parse_credit_rows(CREDIT_GRID)
        self.assertEqual([(r["customer"], r["amount"]) for r in rows],
                         [("Precious Akporeha", "1000.00"), ("VERA AKPOREHA", "534800.00"), ("VERA AKPOREHA", "2750.00")])

    def test_transaction_match_gives_tender_staff_till(self):
        hit = cr.match_transaction({"customer": "VERA AKPOREHA", "when": "09/10/2026 14:19:15", "amount": "534800.00"},
                                   TXN_ROWS)
        self.assertEqual(hit, {"staff": "ERNEST JAITTO", "device": "Till2", "tender": "Transfer"})
        self.assertIsNone(cr.match_transaction({"customer": "VERA AKPOREHA", "when": "09/10/2026 14:19:15",
                                                "amount": "1.00"}, TXN_ROWS))

    def test_customer_report_balances(self):
        self.assertEqual(cr.parse_customer_rows(CUSTOMER_GRID),
                         [{"name": "VERA AKPOREHA", "balance": "-623149.99", "max_credit": "5000000.00",
                           "last_transaction": "25/10/2026 19:02:24"}])

    def test_tender_kind(self):
        self.assertEqual([cr.tender_kind(t) for t in ("Transfer", "Cash", "POS", "Cash/Transfer", "", "Credit")],
                         ["transfer", "cash", "card", "", "", ""])


class PlanTests(unittest.TestCase):
    def test_new_payment_applies_oldest_invoice_first_into_undeposited_funds(self):
        p = cr.plan({"payments": [pay("20/10/2026 12:00:00", "100000.00")]}, qbo(), done={}, banked=set(),
                    limit=Decimal("2000000"))
        row, body = p["rows"][0], p["payloads"][0]
        self.assertEqual(row["status"], cr.READY)
        self.assertEqual(body["DepositToAccountRef"], {"value": "72"})
        self.assertEqual(body["TxnDate"], "2026-10-20")
        self.assertEqual([(l["LinkedTxn"][0]["TxnId"], l["Amount"]) for l in body["Line"]],
                         [("80642", 58200.0), ("80700", 41800.0)])
        self.assertIn(f"| epos-repay {row['key']} |", body["PrivateNote"])
        self.assertLessEqual(len(body["PaymentRefNum"]), 21)

    def test_more_than_owed_leaves_unapplied_credit(self):
        p = cr.plan({"payments": [pay("20/10/2026 12:00:00", "300000.00")]}, qbo(), done={}, banked=set(),
                    limit=Decimal("2000000"))
        self.assertEqual(sum(l["Amount"] for l in p["payloads"][0]["Line"]), 264500.0)
        self.assertIn("unapplied credit N35,500.00", p["rows"][0]["applied_to"])

    def test_vera_9_oct_is_held_because_that_day_is_already_banked(self):
        # 26 Oct 2026: her N534,800 transfer was banked as till overage on 9 Oct; correct it by hand with a yes
        p = cr.plan({"payments": [pay("09/10/2026 14:19:15", "534800.00")]}, qbo(), done={}, banked={"2026-10-09"},
                    limit=Decimal("2000000"))
        self.assertEqual(p["rows"][0]["status"], cr.HOLD)
        self.assertIn("already banked", p["rows"][0]["reason"])
        self.assertEqual(p["payloads"], [])

    def test_holds_unknown_customer_mixed_or_missing_tender(self):
        p = cr.plan({"payments": [pay("20/10/2026 12:00:00", "500.00", customer="SOMEONE NEW"),
                                  pay("20/10/2026 12:01:00", "500.00", tender="Cash/Transfer"),
                                  pay("20/10/2026 12:02:00", "500.00", tender="")]},
                    qbo(), done={}, banked=set(), limit=Decimal("2000000"))
        self.assertEqual([r["status"] for r in p["rows"]], [cr.HOLD] * 3)
        self.assertIn("customer", p["rows"][0]["reason"])
        self.assertIn("mixed", p["rows"][1]["reason"])
        self.assertIn("not found", p["rows"][2]["reason"])

    def test_already_in_quickbooks_or_ledger_is_never_planned_again(self):
        first = cr.plan({"payments": [pay("20/10/2026 12:00:00", "1000.00")]}, qbo(), done={}, banked=set(),
                        limit=Decimal("2000000"))
        key = first["rows"][0]["key"]
        existing = {"Id": "900", "PrivateNote": first["payloads"][0]["PrivateNote"]}
        for kwargs in ({"done": {}, "q": qbo([existing])}, {"done": {key: {"qbo_id": "900"}}, "q": qbo()}):
            p = cr.plan({"payments": [pay("20/10/2026 12:00:00", "1000.00")]}, kwargs["q"], done=kwargs["done"],
                        banked=set(), limit=Decimal("2000000"))
            self.assertEqual((p["rows"][0]["status"], p["rows"][0]["qbo_id"], p["payloads"]), (cr.DONE, "900", []))

    def test_over_limit_waits(self):
        p = cr.plan({"payments": [pay("20/10/2026 12:00:00", "2500000.00")]}, qbo(), done={}, banked=set(),
                    limit=Decimal("2000000"))
        self.assertEqual(p["rows"][0]["status"], cr.WAIT)


class SettingTests(unittest.TestCase):
    def test_off_by_default_then_settings_file_then_environment_wins(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(cr, "settings_path", return_value=tmp / "settings.env"):
            self.assertEqual(cr.setting({}), "off")
            (tmp / "settings.env").write_text(f"# owner switch\n{cr.MODE_ENV}=plan  # read and plan only\n")
            self.assertEqual((cr.setting({}), cr.mode({})), ("plan", "plan"))
            self.assertEqual(cr.setting({cr.MODE_ENV: "post"}), "post")
            (tmp / "settings.env").write_text(f"{cr.MODE_ENV}=nonsense\n")
            self.assertEqual(cr.setting({}), "off")


class FakeQBO:
    def __init__(self):
        self.payments, self.n = [], 900

    def query_all(self, sql, entity):
        day = sql.split("'")[1]
        return [p for p in self.payments if p["TxnDate"] == day]

    def post_json(self, path, body, requestid):
        self.n += 1
        rec = dict(body, Id=str(self.n))
        self.payments.append(rec)
        return {"Payment": rec}

    def get_json(self, path):
        pid = path.rsplit("/", 1)[1]
        return {"Payment": next(p for p in self.payments if p["Id"] == pid)}


class PostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"STATE_ROOT": str(self.tmp / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        mock.patch.object(cr, "ledger_path", return_value=self.tmp / "posted.json").start()
        self.addCleanup(mock.patch.stopall)

    def test_post_once_then_retry_finds_it(self):
        data = {"payments": [pay("20/10/2026 12:00:00", "1000.00")]}
        p = cr.plan(data, qbo(), done={}, banked=set(), limit=Decimal("2000000"))
        s = cr.write_plan(self.tmp / "plan", data, p)
        fake = FakeQBO()
        with self.assertRaises(cr.StopPost):
            cr.post(self.tmp / "plan", client=fake, approval_ref="yes", expect_sha="0" * 64)
        r = cr.post(self.tmp / "plan", client=fake, approval_ref="owner yes", expect_sha=s["payloads_sha256"])
        self.assertEqual([x["status"] for x in r["results"]], [cr.POSTED])
        self.ledger = json.loads((self.tmp / "posted.json").read_text())
        (self.tmp / "posted.json").unlink()  # lost ledger: the QBO tag still prevents a second payment
        r = cr.post(self.tmp / "plan", client=fake, approval_ref="owner yes", expect_sha=s["payloads_sha256"])
        self.assertEqual([x["status"] for x in r["results"]], ["ALREADY_DONE"])
        self.assertEqual(len(fake.payments), 1)


if __name__ == "__main__":
    unittest.main()


DEP_9OCT = {"Id": "80700", "TxnDate": "2026-10-09", "SyncToken": "0", "TotalAmt": 2547300.0,
            "PrivateNote": "UF deposit 2026-10-09 from till sheet | UF261009100202 -> 100202 | created by uf_deposits",
            "DepositToAccountRef": {"value": "1150040002"},
            "Line": [{"Id": "1", "Amount": 2012050.0, "LinkedTxn": [{"TxnId": "501", "TxnType": "SalesReceipt"}]},
                     {"Id": "2", "Amount": 535250.0, "DetailType": "DepositLineDetail",
                      "DepositLineDetail": {"AccountRef": {"value": "800"}}}]}


class CorrectionQBO(FakeQBO):
    def __init__(self):
        super().__init__()
        self.deposits = {"80700": json.loads(json.dumps(DEP_9OCT))}

    def post_json(self, path, body, requestid):
        if path == "/deposit":
            dep = dict(body, SyncToken=str(int(body["SyncToken"]) + 1),
                       TotalAmt=round(sum(l["Amount"] for l in body["Line"]), 2))
            self.deposits[dep["Id"]] = dep
            return {"Deposit": dep}
        return super().post_json(path, body, requestid)

    def get_json(self, path):
        if path.startswith("/deposit/"):
            return {"Deposit": json.loads(json.dumps(self.deposits[path.rsplit("/", 1)[1]]))}
        return super().get_json(path)


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        mock.patch.object(cr, "ledger_path", return_value=self.tmp / "posted.json").start()
        self.addCleanup(mock.patch.stopall)

    def plan(self, deposits=(DEP_9OCT,), amount="534800.00"):
        data = {"payments": [pay("09/10/2026 14:19:15", amount)]}
        return cr.plan_corrections(data, qbo(), list(deposits), "800", done={}, banked={"2026-10-09"},
                                   limit=Decimal("2000000"))

    def test_vera_9_oct_moves_out_of_that_days_till_overage(self):
        p = self.plan()
        self.assertEqual(len(p["items"]), 1)
        item = p["items"][0]
        self.assertEqual((item["deposit_id"], item["overage_line_id"], item["amount"]), ("80700", "2", "534800.00"))
        self.assertIn("move N534,800.00 out of till overage", p["rows"][0]["reason"])

    def test_more_than_the_overage_is_held(self):
        p = self.plan(amount="600000.00")
        self.assertEqual((p["items"], p["rows"][0]["status"]), ([], cr.HOLD))
        self.assertIn("no 2026-10-09 UF deposit has", p["rows"][0]["reason"])

    def test_post_links_payment_lowers_overage_keeps_total_and_is_safe_to_repeat(self):
        p = self.plan()
        s = cr.write_corrections(self.tmp / "c", p)
        fake = CorrectionQBO()
        r = cr.post_corrections(self.tmp / "c", client=fake, approval_ref="owner yes", expect_sha=s["sha256"])
        self.assertTrue(r["complete"], r)
        dep = fake.deposits["80700"]
        self.assertEqual(dep["TotalAmt"], 2547300.0)  # the bank is unchanged
        over = [l for l in dep["Line"] if not l.get("LinkedTxn")]
        self.assertEqual([l["Amount"] for l in over], [450.0])  # 535,250 - 534,800 of real overage left
        pid = fake.payments[0]["Id"]
        self.assertIn({"TxnId": pid, "TxnType": "Payment", "TxnLineId": "0"},
                      [t for l in dep["Line"] for t in l.get("LinkedTxn") or []])
        again = cr.post_corrections(self.tmp / "c", client=fake, approval_ref="owner yes", expect_sha=s["sha256"])
        self.assertEqual([x["status"] for x in again["results"]], ["ALREADY_DONE"])
        self.assertEqual((len(fake.payments), fake.deposits["80700"]["TotalAmt"]), (1, 2547300.0))

    def test_a_deposit_changed_after_the_plan_stops(self):
        p = self.plan()
        s = cr.write_corrections(self.tmp / "c", p)
        fake = CorrectionQBO()
        fake.deposits["80700"]["SyncToken"] = "5"
        r = cr.post_corrections(self.tmp / "c", client=fake, approval_ref="owner yes", expect_sha=s["sha256"])
        self.assertFalse(r["complete"])
        self.assertIn("changed since the plan", r["results"][-1]["detail"])
        self.assertEqual(fake.deposits["80700"]["Line"][1]["Amount"], 535250.0)
        self.assertEqual(fake.payments, [])  # nothing written when the deposit moved on

    def test_payment_made_but_deposit_not_fixed_is_planned_again_and_finished_once(self):
        p = self.plan()
        s = cr.write_corrections(self.tmp / "c", p)
        fake = CorrectionQBO()
        real_post = fake.post_json
        fake.post_json = lambda path, body, rid: (_ for _ in ()).throw(RuntimeError("timeout")) if path == "/deposit" \
            else real_post(path, body, rid)
        r = cr.post_corrections(self.tmp / "c", client=fake, approval_ref="owner yes", expect_sha=s["sha256"])
        self.assertFalse(r["complete"])
        self.assertEqual(len(fake.payments), 1)
        fake.post_json = real_post
        led = json.loads((self.tmp / "posted.json").read_text())
        q = qbo([fake.payments[0]])
        p2 = cr.plan_corrections({"payments": [pay("09/10/2026 14:19:15", "534800.00")]}, q, [DEP_9OCT], "800",
                                 done=led, banked={"2026-10-09"}, limit=Decimal("2000000"))
        self.assertEqual(len(p2["items"]), 1)  # not treated as done: its deposit change is still owed
        s2 = cr.write_corrections(self.tmp / "c2", p2)
        r2 = cr.post_corrections(self.tmp / "c2", client=fake, approval_ref="owner yes", expect_sha=s2["sha256"])
        self.assertTrue(r2["complete"], r2)
        self.assertEqual(len(fake.payments), 1)  # the same Payment, linked now
        self.assertEqual([l["Amount"] for l in fake.deposits["80700"]["Line"] if not l.get("LinkedTxn")], [450.0])
