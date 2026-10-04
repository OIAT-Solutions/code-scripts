"""Cash-on-delivery bills are paid from Petty Cash (owner yes 4 Oct 2026). No network."""
from __future__ import annotations

import json
import unittest

from code_scripts.akponora_ops import bill_payments as bp


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeQBO:
    def __init__(self, bills, fail=False):
        self.bills = {b["Id"]: dict(b) for b in bills}
        self.payments, self.fail = [], fail

    def query_all(self, sql, entity):
        if entity == "Account":
            return [{"Id": "1150040002", "Name": "MONIEPOINT 4686987227"}, {"Id": "1150040001", "Name": "MONIEPOINT 4000700275"},
                    {"Id": "1150040041", "Name": "MONIEPOINT 4000850527"}, {"Id": "29", "Name": "Petty Cash"}]
        assert entity == "Bill" and "TxnDate >= '2026-10-01'" in sql
        return [dict(b) for b in self.bills.values() if b["TxnDate"] >= "2026-10-01"]

    def post_json(self, path, body, requestid):
        assert path == "/billpayment" and requestid
        if self.fail:
            return Resp(400, {"Fault": "boom"})
        self.payments.append(body)
        for line in body["Line"]:
            bill = self.bills[line["LinkedTxn"][0]["TxnId"]]
            bill["Balance"] = round(bill["Balance"] - line["Amount"], 2)
        return Resp(200, {"BillPayment": {"Id": str(900 + len(self.payments))}})

    def get_json(self, path):
        return {"Bill": dict(self.bills[path.rsplit("/", 1)[1]])}


def bill(i, *, date="2026-10-03", hint="CASH", balance=10000.0, doc=None, vendor="261"):
    return {"Id": str(i), "TxnDate": date, "Balance": balance, "TotalAmt": balance,
            "DocNumber": doc if doc is not None else f"EPOS-PO-{i}", "VendorRef": {"value": vendor, "name": "V"},
            "PrivateNote": f"Payment hint: {hint} (EPOS PO note; bill left UNPAID - pay in QBO) | EPOS PO {i}"}


class CashPaymentTests(unittest.TestCase):
    def test_only_open_october_cash_bills_from_the_bills_job_are_paid(self):
        fake = FakeQBO([bill(3978), bill(3979, date="2026-10-04"), bill(3972, hint="TRANSFER"),
                        bill(3960, hint="not stated"), bill(1, doc="MANUAL-1"), bill(3900, date="2026-09-30"),
                        bill(3971, balance=0.0)])
        res = bp.pay_cash_bills(fake, env={})
        self.assertEqual([r["doc"] for r in res["paid"]], ["EPOS-PO-3978", "EPOS-PO-3979"])
        self.assertEqual(res["total"], "20000.00")
        p = fake.payments[0]
        self.assertEqual(p["CheckPayment"]["BankAccountRef"]["value"], "29")  # 100100 Petty Cash
        self.assertEqual(p["TxnDate"], "2026-10-03")
        self.assertEqual(p["Line"][0]["LinkedTxn"], [{"TxnId": "3978", "TxnType": "Bill"}])
        self.assertIn("Paid on delivery per EPOS PO 3978 (Payment hint: CASH)", p["PrivateNote"])
        # a second run pays nothing (the bills are at balance 0)
        self.assertEqual(bp.pay_cash_bills(fake, env={})["paid"], [])
        self.assertEqual(len(fake.payments), 2)

    def test_po_note_convention_paid_not_paid_and_account(self):
        fake = FakeQBO([bill(10, hint="CASH NOT PAID"), bill(11, hint="TRANSFER PAID from 4686"),
                        bill(12, hint="TRANSFER PAID"), bill(13, hint="TRANSFER PAID from 4000"),
                        bill(14, hint="CREDIT"), bill(15, hint="CASH PAID")])
        res = bp.pay_cash_bills(fake, env={})
        self.assertEqual({r["doc"]: r["account"] for r in res["paid"]},
                         {"EPOS-PO-11": "1150040002", "EPOS-PO-15": "29"})
        # 13: '4000' matches two Moniepoint accounts -> left open; 12: no account -> open; 10/14 not paid

    def test_bills_sync_reads_the_convention_from_the_po_note(self):
        from code_scripts.akponora_ops import bills_sync as bs
        cases = {"SUPPLIER: X MODE OF PAYMENT: CASH (PAID)": ("PAID", ""),
                 "SUPPLIER: X MODE OF PAYMENT: TRANSFER (PAID, MONIEPOINT 4686)": ("PAID", "4686"),
                 "SUPPLIER: X MODE OF PAYMENT:TRANSFER(PAID)": ("PAID", ""),
                 "SUPPLIER: X MODE OF PAYMENT: CASH NOT PAID": ("NOT PAID", ""),
                 "SUPPLIER: X MODE OF PAYMENT: CREDIT": ("NOT PAID", ""),
                 "SUPPLIER: X MODE OF PAYMENT:TRANSFER": ("", ""),
                 "SUPPLIER: X": ("", "")}
        for note, want in cases.items():
            self.assertEqual(bs.payment_detail(note), want, note)

    def test_caps_off_switch_dry_run_and_failure(self):
        fake = FakeQBO([bill(1), bill(2), bill(3, balance=2500000.0)])
        res = bp.pay_cash_bills(fake, env={bp.MAX_ENV: "1"})
        self.assertEqual(len(res["paid"]), 1)
        self.assertEqual(len(res["capped"]), 2)  # one over the count cap, one over the amount cap
        self.assertFalse(bp.pay_cash_bills(fake, env={bp.ENABLED_ENV: "0"})["enabled"])
        self.assertEqual([r["doc"] for r in bp.pay_cash_bills(fake, env={}, dry_run=True)["planned"]], ["EPOS-PO-2"])
        failing = FakeQBO([bill(5), bill(6)], fail=True)
        res = bp.pay_cash_bills(failing, env={})
        self.assertEqual(len(res["failed"]), 1)  # stops at the first problem
        self.assertEqual(res["paid"], [])


if __name__ == "__main__":
    unittest.main()
