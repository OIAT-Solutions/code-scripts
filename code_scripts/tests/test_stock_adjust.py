"""stock_adjust: checked count draft -> approved QuickBooks InventoryAdjustment (fake QBO, no network)."""
import json
import tempfile
import unittest
from pathlib import Path

from code_scripts.akponora_ops import stock_adjust as sa
from code_scripts.akponora_ops.stock_reconciliation import count_plan


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeQBO:
    def __init__(self):
        self.items = {"101": {"Id": "101", "Name": "Coke 60cl", "Sku": "AKP-1", "Type": "Inventory", "Active": True,
                              "TrackQtyOnHand": True, "QtyOnHand": -4},
                      "102": {"Id": "102", "Name": "Eva soap", "Sku": "AKP-2", "Type": "Inventory", "Active": True,
                              "TrackQtyOnHand": True, "QtyOnHand": 10}}
        self.accounts = {"82": {"Id": "82", "Name": "Inventory Shrinkage", "AccountType": "Cost of Goods Sold", "Active": True},
                         "77": {"Id": "77", "Name": "Inventory Asset", "AccountType": "Other Current Asset", "Active": True}}
        self.adjustments, self.posts, self.fail = {}, [], False

    def get_json(self, path, params=None):
        kind, key = path.strip("/").split("/")
        if kind == "item":
            return {"Item": dict(self.items[key])}
        if kind == "account":
            return {"Account": dict(self.accounts[key])}
        return {"InventoryAdjustment": self.adjustments[key]}

    def query_all(self, sql, entity):
        day = sql.split("TxnDate = '")[1][:10]
        return [a for a in self.adjustments.values() if a["TxnDate"] == day]

    def post_json(self, path, body, requestid):
        assert path == "/inventoryadjustment" and requestid
        self.posts.append(body)
        if self.fail:
            return Resp(400, {"Fault": {"Error": [{"Detail": "boom"}]}})
        made = dict(body, Id=str(900 + len(self.adjustments)))
        self.adjustments[made["Id"]] = made
        for ln in body["Line"]:
            d = ln["ItemAdjustmentLineDetail"]
            self.items[d["ItemRef"]["value"]]["QtyOnHand"] += d["QtyDiff"]
        return Resp(200, {"InventoryAdjustment": made})


def draft(account="82", **over):
    snap = {"company": "company_a", "rows": [
        {"family_sku": "AKP-1", "type": "Inventory", "qbo_active": True, "qbo_item_id": "101", "flags": []},
        {"family_sku": "AKP-2", "type": "Inventory", "qbo_active": True, "qbo_item_id": "102", "flags": []}]}
    ev = {"cutoff": "2026-10-08", "count_source": "shelf count by Esther", "confirmed_by": "Esther",
          "reason": "stock take 5621722 was a count correction", "transactions_reconciled_by": "OIAT 8 Oct"}
    decisions = [dict(ev, sku="AKP-1", verified_qty="20", qbo_qty_at_cutoff="-4"),
                 dict(ev, sku="AKP-2", verified_qty="10", qbo_qty_at_cutoff="10")]
    d = count_plan(snap, decisions, cutoff="2026-10-08", offset_account_id=account,
                   approval_basis="Count variance to 82 Inventory Shrinkage (accountant notes 6 Oct)")
    d.update(over)
    return d


class StockAdjustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.qbo = FakeQBO()

    def test_plan_then_post_once_and_verify(self):
        s = sa.plan(draft(), self.qbo, self.tmp)
        self.assertEqual(s["status"], sa.READY, s["holds"])
        self.assertEqual((s["lines"], s["units_added"], s["units_removed"]), (1, "24", "0"))  # unchanged line left out
        p = json.loads((self.tmp / "payloads.jsonl").read_text())
        self.assertEqual(p["AdjustAccountRef"], {"value": "82"})
        self.assertEqual(p["Line"][0]["ItemAdjustmentLineDetail"], {"ItemRef": {"value": "101"}, "QtyDiff": 24.0})
        self.assertLessEqual(len(p["DocNumber"]), 21)
        with self.assertRaises(sa.StopPost):
            sa.post(self.tmp, client=self.qbo, approval_ref="yes", expect_sha="0" * 64)
        with self.assertRaises(sa.StopPost):
            sa.post(self.tmp, client=self.qbo, approval_ref="", expect_sha=s["payloads_sha256"])
        r = sa.post(self.tmp, client=self.qbo, approval_ref="owner yes 8 Oct", expect_sha=s["payloads_sha256"])
        self.assertTrue(r["complete"])
        self.assertEqual(r["warnings"], [])
        self.assertEqual(self.qbo.items["101"]["QtyOnHand"], 20)
        self.assertIn("approval owner yes 8 Oct", self.qbo.posts[0]["PrivateNote"])
        again = sa.post(self.tmp, client=self.qbo, approval_ref="owner yes 8 Oct", expect_sha=s["payloads_sha256"])
        self.assertEqual([x["status"] for x in again["results"]], [sa.ALREADY_DONE])
        self.assertEqual(len(self.qbo.posts), 1)
        self.assertEqual(sa.plan(draft(), self.qbo, self.tmp / "replan")["status"], sa.DONE)

    def test_holds(self):
        self.assertEqual(sa.plan(draft(account="77"), self.qbo, self.tmp / "a")["status"], sa.HOLD)  # not COGS
        self.qbo.items["101"]["Sku"] = "AKP-9"
        self.assertEqual(sa.plan(draft(), self.qbo, self.tmp / "b")["status"], sa.HOLD)
        self.qbo.items["101"]["Sku"] = "AKP-1"
        self.qbo.items["101"]["TrackQtyOnHand"] = False
        self.assertEqual(sa.plan(draft(), self.qbo, self.tmp / "c")["status"], sa.HOLD)

    def test_changed_draft_is_refused(self):
        d = draft()
        d["entries"][0]["verified_quantity"] = "200"
        with self.assertRaises(sa.StopPost):
            sa.plan(d, self.qbo, self.tmp)

    def test_failed_post_stops_and_is_reported(self):
        s = sa.plan(draft(), self.qbo, self.tmp)
        self.qbo.fail = True
        r = sa.post(self.tmp, client=self.qbo, approval_ref="yes", expect_sha=s["payloads_sha256"])
        self.assertFalse(r["complete"])
        self.assertEqual(r["results"][0]["status"], sa.FAILED)

    def test_quantity_moved_by_something_else_is_flagged(self):
        s = sa.plan(draft(), self.qbo, self.tmp)
        real = self.qbo.post_json

        def post_and_sell(path, body, requestid):
            resp = real(path, body, requestid)
            self.qbo.items["101"]["QtyOnHand"] -= 1  # a sale lands between post and read-back
            return resp
        self.qbo.post_json = post_and_sell
        r = sa.post(self.tmp, client=self.qbo, approval_ref="yes", expect_sha=s["payloads_sha256"])
        self.assertTrue(r["complete"])
        self.assertTrue(r["warnings"])


if __name__ == "__main__":
    unittest.main()
