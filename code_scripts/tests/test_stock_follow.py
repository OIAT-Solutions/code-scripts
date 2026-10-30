"""QuickBooks follows EPOS Stock Take / Wastage adjustments (fake QBO, no network)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_scripts.akponora_ops import stock_follow as sf


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, json.dumps(body)

    def json(self):
        return self._b


class FakeQBO:
    def __init__(self):
        self.qty = {"101": -1.0, "102": 5.0}
        self.adjustments, self.posts, self.fail = {}, [], False

    def get_json(self, path, params=None):
        return {"Item": {"Id": path.rsplit("/", 1)[1], "QtyOnHand": self.qty[path.rsplit("/", 1)[1]]}}

    def query_all(self, sql, entity):
        day = sql.split("TxnDate = '")[1][:10]
        return [a for a in self.adjustments.values() if a["TxnDate"] == day]

    def post_json(self, path, body, requestid):
        assert path == "/inventoryadjustment" and requestid
        self.posts.append(body)
        if self.fail:
            return Resp(400, {"Fault": {}})
        made = dict(body, Id=str(900 + len(self.adjustments)))
        self.adjustments[made["Id"]] = made
        for ln in body["Line"]:
            d = ln["ItemAdjustmentLineDetail"]
            self.qty[d["ItemRef"]["value"]] += d["QtyDiff"]
        return Resp(200, {"InventoryAdjustment": made})


def event(eid, reason="Stock Take", qty="20", item="101", when="08/10/2026 13:23:27", value="₦7,879.84", holds=()):
    return {"event_id": eid * 8, "transfer_id": str(5680846 + sum(ord(c) for c in eid)), "occurred_at_epos": when, "staff": "Esther Aribatis",
            "note": "", "name": "DUBIC MALT CAN 330ml*24", "epos_product_id": "1", "sku": "AKP-1", "qbo_item_id": item,
            "canonical_qty_change": qty, "epos_reason": reason, "source_value_change": value, "holds": list(holds)}


class StockFollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(sf, "state_dir", lambda tool: self.tmp / "state" / tool)
        p.start()
        self.addCleanup(p.stop)
        (self.tmp / "state" / sf.TOOL).mkdir(parents=True)
        self.qbo, self.s = FakeQBO(), sf.settings({})

    def capture(self, events, name="daily/2026-10-09/run_1/movements/summary.json"):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"company": "company_a", "events": events}))
        return path

    def test_rules(self):
        cases = [(event("a"), "ST", ""), (event("b", reason="Wastage", qty="-2"), "WS", ""),
                 (event("c", reason="New Stock"), "NA", "delivery/PO review"), (event("d", reason="n/a"), "NA", "delivery/PO review"),
                 (event("d2", reason="Returns"), "", "is not Stock Take, Wastage"),
                 (event("e", holds=["child unit"]), "ST", "not certain"),
                 (event("f", value="-₦150,000.00"), "ST", "over the N100,000 limit")]
        evs = sf.collect([self.capture([c[0] for c in cases])])
        for e, kind, why in cases:
            k, w = sf.classify(evs[e["event_id"]], self.s["cap"])
            self.assertEqual(k, kind)
            self.assertIn(why, w) if why else self.assertEqual(w, "")

    def test_no_reason_changes_wait_for_delivery_review(self):
        evs = sf.collect([self.capture([event("n", reason="n/a", qty="24")])])
        summary = sf.plan(evs, self.tmp / "na", s=self.s, approve_over_cap=False)
        self.assertEqual(summary["events"], 0)
        self.assertEqual(summary["review_count"], 1)
        self.assertEqual((self.tmp / "na/payloads.jsonl").read_text(), "")

    def test_lagos_day_by_capture_zone(self):
        self.assertEqual(sf.lagos_day("08/10/2026 23:30:00", "daily/x"), "2026-10-09")  # UTC 23:30 -> Lagos 00:30
        self.assertEqual(sf.lagos_day("08/10/2026 19:30:00", "portal_reads/stock_recovery_20261006/m"), "2026-10-09")

    def test_plan_post_once_and_verify(self):
        evs = sf.collect([self.capture([event("a"), event("b", reason="Wastage", qty="-2", item="102"),
                                        event("f", value="₦150,000.00")])])
        out = self.tmp / "plan"
        s = sf.plan(evs, out, s=self.s, approve_over_cap=False, tag="C")
        self.assertEqual((s["status"], s["events"], s["adjustments"]), (sf.READY, 2, 2))
        pl = [json.loads(x) for x in (out / "payloads.jsonl").read_text().splitlines()]
        self.assertEqual({p["AdjustAccountRef"]["value"] for p in pl}, {"82", "83"})
        self.assertTrue(all(p["DocNumber"][:2] in ("ST", "WS", "NA") and len(p["DocNumber"]) <= 21 for p in pl))
        with self.assertRaises(sf.StopPost):
            sf.post(out, client=self.qbo, approval_ref="yes", expect_sha="0" * 64)
        r = sf.post(out, client=self.qbo, approval_ref="owner yes", expect_sha=s["payloads_sha256"])
        self.assertTrue(r["complete"])
        self.assertEqual((self.qbo.qty["101"], self.qbo.qty["102"]), (19.0, 3.0))
        self.assertEqual(len(sf.ledger()), 2)
        again = sf.plan(evs, self.tmp / "plan2", s=self.s, approve_over_cap=False, tag="C")
        self.assertEqual(again["status"], sf.DONE)  # nothing left to post
        big = sf.plan(evs, self.tmp / "big", s=self.s, approve_over_cap=True, tag="CL")
        self.assertEqual(big["events"], 1)

    def test_same_item_twice_in_a_day_is_one_line(self):
        # 11 Oct 2026: QBO refused "item ... is used more than once in this Inventory Quantity Adjustment"
        e2 = dict(event("b", qty="-3"), transfer_id="5680999")
        evs = sf.collect([self.capture([event("a"), e2])])
        s = sf.plan(evs, self.tmp / "p", s=self.s, approve_over_cap=False)
        p = json.loads((self.tmp / "p" / "payloads.jsonl").read_text().splitlines()[0])
        self.assertEqual(len(p["Line"]), 1)
        self.assertEqual(p["Line"][0]["ItemAdjustmentLineDetail"]["QtyDiff"], 17.0)
        self.assertEqual(len(p["_events"]), 2)
        r = sf.post(self.tmp / "p", client=self.qbo, approval_ref="yes", expect_sha=s["payloads_sha256"])
        self.assertTrue(r["complete"])
        self.assertEqual(self.qbo.qty["101"], 16.0)

    def test_adjustment_already_in_qbo_is_recorded_not_reposted(self):
        evs = sf.collect([self.capture([event("a")])])
        s = sf.plan(evs, self.tmp / "p", s=self.s, approve_over_cap=False)
        sf.post(self.tmp / "p", client=self.qbo, approval_ref="yes", expect_sha=s["payloads_sha256"])
        sf.ledger_path().unlink()  # ledger lost after the post
        s2 = sf.plan(evs, self.tmp / "p2", s=self.s, approve_over_cap=False)
        r = sf.post(self.tmp / "p2", client=self.qbo, approval_ref="yes", expect_sha=s2["payloads_sha256"])
        self.assertEqual(r["results"][0]["status"], "ALREADY_DONE")
        self.assertEqual(len(self.qbo.posts), 1)

    def test_nightly_waits_for_catchup_then_posts_within_cap(self):
        path = self.capture([event("a"), event("f", value="₦150,000.00")])
        with mock.patch.object(sf, "ops_root", lambda: self.tmp):
            r = sf.nightly(path, self.tmp / "n1", client=self.qbo, write_client=self.qbo, env={})
            self.assertIn("catch-up", r["note"])
            self.assertEqual(self.qbo.posts, [])
            (self.tmp / "state" / sf.TOOL / "catchup_posted.json").write_text("{}")
            r = sf.nightly(path, self.tmp / "n2", client=self.qbo, write_client=self.qbo, env={})
        self.assertEqual(len(self.qbo.posts), 1)
        self.assertEqual(r["waiting_approval"], 1)
        self.assertTrue((self.tmp / "portal_reads" / "stock_follow").exists())


class DailyRunStepTests(unittest.TestCase):
    def test_step_skips_without_capture_and_reports_counts(self):
        from code_scripts.akponora_ops import daily_run as dr
        with tempfile.TemporaryDirectory() as folder:
            run = dr.DailyRun("2026-10-09", root=Path(folder), runner=lambda *a, **k: 0, env={})
            res = dr.StepResult(name="stock_follow", out=str(run.step_dir("stock_follow")))
            run.step_stock_follow(res)
            self.assertEqual(res.status, dr.SKIPPED)
            (run.step_dir("movements") / "summary.json").write_text("{}")
            fake = {"events": 3, "posted": [{"status": "POSTED"}], "waiting_approval": 1, "not_automatic": 1, "note": ""}
            with mock.patch.object(sf, "nightly", return_value=fake), mock.patch.object(sf, "catchup_done", return_value=False):
                run.step_stock_follow(res)
            self.assertEqual(res.status, dr.REVIEW)
            self.assertIn("1 QuickBooks adjustment(s) posted", dr.step_line(res.__dict__))

if __name__ == "__main__":
    unittest.main()
