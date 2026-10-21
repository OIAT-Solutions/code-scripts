"""Portal approval contracts: catalogue_sync selective apply, review exclusions, vendor approve, bills skip.

All QBO / EPOS access is faked. Real network access fails the test loudly (socket connect and
requests are patched for this module; ``QBOClient.for_company_a`` raises).
"""
import csv
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

import requests

from code_scripts.akponora_ops import bills_sync as bs
from code_scripts.akponora_ops import catalogue_sync as cs
from code_scripts.akponora_ops import review_exclusions as rx
from code_scripts.akponora_ops import vendors as vendor_ops
from code_scripts.product_conversion import ProductConversionRegistry
from code_scripts.tests.test_akponora_daily_run import VendorFakeQBO
from code_scripts.tests.test_bills_sync import WATER, Fixture, client, order
from code_scripts.tests.test_catalogue_sync import Base as CatalogueBase
from code_scripts.tests.test_catalogue_sync import GROCERY, product, scrape

_patches = []


def _no_network(*_a, **_k):
    raise AssertionError("real network access attempted in a test")


def setUpModule():
    for target, attr in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                         (requests.Session, "request"), (requests.sessions.Session, "send")):
        p = mock.patch.object(target, attr, _no_network)
        p.start()
        _patches.append(p)
    p = mock.patch.object(cs.w7.QBOClient, "for_company_a", side_effect=_no_network)
    p.start()
    _patches.append(p)


def tearDownModule():
    while _patches:
        _patches.pop().stop()


class NetworkGuardTests(unittest.TestCase):
    def test_real_http_fails_loudly(self):
        with self.assertRaisesRegex(AssertionError, "real network"):
            requests.get("https://quickbooks.api.intuit.com/")
        with self.assertRaisesRegex(AssertionError, "real network"):
            cs.w7.QBOClient.for_company_a(allow_writes=False)


# ---------------------------------------------------------------- review exclusions
class ReviewExclusionsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / rx.FILE_NAME

    def history(self):
        with open(rx.history_path(self.path), encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_add_list_remove_with_append_only_history(self):
        res = rx.add("vendor", "  Wonuola  Super-Store ", reason="owner buys these personally",
                     added_by="portal approval 12", path=self.path)
        self.assertEqual(res["result"], "added")
        self.assertEqual(res["row"]["key"], rx.normalize_key("vendor", "WONUOLA SUPER STORE"))
        rx.add("bill", "EPOS-PO-3990", reason="paid in cash, entered by bookkeeper", added_by="r1", path=self.path)
        rx.add("product", " 123 ", reason="test product", added_by="r2", expires_at="2099-01-01", path=self.path)
        ex = rx.load(self.path)
        self.assertIsNotNone(ex.get("bill", "3990"))
        self.assertIsNotNone(ex.get("vendor", "wonuola super store"))
        self.assertIsNotNone(ex.get("product", "123"))
        self.assertEqual(len(rx.list_rows(path=self.path)), 3)
        self.assertEqual([r["key"] for r in rx.list_rows(kind="bill", path=self.path)], ["3990"])
        rx.remove("bill", "3990", removed_by="r3", reason="mistake", path=self.path)
        self.assertIsNone(rx.load(self.path).get("bill", "3990"))
        hist = self.history()
        self.assertEqual([h["action"] for h in hist], ["add", "add", "add", "remove"])
        self.assertEqual(hist[-1]["actor"], "r3")
        self.assertTrue(all(h["file_sha256_after"] for h in hist))

    def test_add_is_idempotent_and_refuses_a_different_row_without_replace(self):
        rx.add("product", "55", reason="a", added_by="r", path=self.path)
        self.assertEqual(rx.add("product", "55", reason="a", added_by="r", path=self.path)["result"], "unchanged")
        with self.assertRaisesRegex(rx.ExclusionError, "already excluded"):
            rx.add("product", "55", reason="b", added_by="r", path=self.path)
        self.assertEqual(rx.add("product", "55", reason="b", added_by="r", replace=True, path=self.path)["result"],
                         "replaced")
        self.assertEqual(rx.load(self.path).get("product", "55")["reason"], "b")
        self.assertEqual([h["action"] for h in self.history()], ["add", "replace"])

    def test_validation_and_expiry(self):
        for kwargs in ({"reason": "", "added_by": "r"}, {"reason": "x", "added_by": ""},
                       {"reason": "x", "added_by": "r", "expires_at": "soon"}):
            with self.assertRaises(rx.ExclusionError):
                rx.add("product", "1", path=self.path, **kwargs)
        with self.assertRaises(rx.ExclusionError):
            rx.add("customer", "1", reason="x", added_by="r", path=self.path)
        with self.assertRaisesRegex(rx.ExclusionError, "not excluded"):
            rx.remove("product", "1", removed_by="r", reason="x", path=self.path)
        rx.add("product", "9", reason="x", added_by="r", expires_at="2026-10-05", path=self.path)
        self.assertIsNotNone(rx.load(self.path, today=date(2026, 10, 4)).get("product", "9"))
        self.assertIsNone(rx.load(self.path, today=date(2026, 10, 5)).get("product", "9"))

    def test_cli_add_list_remove_and_refusal(self):
        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch("code_scripts.scripts.akponora_cutover._common.setup_env"):
                rc = rx.main(["--file", str(self.path), *argv])
            return rc, json.loads(buf.getvalue())

        rc, out = run("add", "--kind", "bill", "--key", "4001", "--reason", "resolved by hand", "--added-by", "R1")
        self.assertEqual((rc, out["result"]), (0, "added"))
        rc, out = run("list", "--kind", "bill")
        self.assertEqual((rc, [r["key"] for r in out["rows"]]), (0, ["4001"]))
        rc, out = run("add", "--kind", "bill", "--key", "4001", "--reason", "other", "--added-by", "R1")
        self.assertEqual((rc, out["result"]), (2, "refused"))
        rc, out = run("remove", "--kind", "bill", "--key", "4001", "--removed-by", "R2", "--reason", "undo")
        self.assertEqual((rc, out["result"]), (0, "removed"))


# ---------------------------------------------------------------- catalogue_sync selection + exclusions
class CatalogueSelectionTests(CatalogueBase):
    def new_products(self):
        return ([product("500", "PEAK MILK*48", True, vos=48, cost=4800.0, price=9600.0),
                 product("501", "PEAK MILK", False), product("502", "SUYA SPICE", False, category=GROCERY),
                 product("503", "WATER 75CL x6", False)],
                {"500": scrape("500", stock=0), "501": scrape("501", [("500", "1Each of 48Each")]),
                 "502": scrape("502"), "503": scrape("503", [("200", "6Each of 1Each")])})

    def sel_apply(self, plan, **kw):
        return cs.apply_plan(self.tmp / "plan", approval_ref=kw.pop("approval", "portal approval 7"),
                             expect_sha=plan["plan_sha256"], automated=kw.pop("automated", False),
                             client=client(self.fake, writes=True), mapping_path=self.mapping, state=self.state,
                             slack=False, **kw)

    def mapped(self):
        return set(ProductConversionRegistry.from_csv(self.mapping).by_product_id)

    def test_plan_carries_per_decision_digests(self):
        plan = self.plan(*self.new_products())
        summary = json.loads((self.tmp / "plan" / "summary.json").read_text())
        self.assertEqual(set(summary["decision_shas"]), {"500", "501", "502", "503"})
        for d in plan["decisions"]:
            self.assertEqual(d["decision_sha256"], cs.decision_digest(d))
        rows = bs.read_csv(self.tmp / "plan" / "review.csv")
        self.assertTrue(all(r["Decision SHA"] for r in rows if r["Status"] == "NEW"))
        again = self.plan(*self.new_products(), name="plan_again")
        self.assertEqual(again["decision_shas"], plan["decision_shas"])

    def test_only_applies_exactly_the_selection_and_receipt_lists_it(self):
        plan = self.plan(*self.new_products())
        receipt = self.sel_apply(plan, only=["500", "501"])
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual([x["pid"] for x in receipt["applied"]], ["500", "501"])
        self.assertEqual(receipt["selection"]["mode"], "explicit")
        self.assertEqual(set(receipt["selection"]["decision_shas"]), {"500", "501"})
        self.assertEqual({x["pid"] for x in receipt["not_selected"]}, {"502", "503"})
        self.assertTrue({"500", "501"} <= self.mapped())
        self.assertFalse({"502", "503"} & self.mapped())
        saved = json.loads((self.tmp / "plan" / "apply_receipt.json").read_text())
        self.assertEqual([x["pid"] for x in saved["applied"]], ["500", "501"])

    def test_exclude_flag_applies_the_rest(self):
        plan = self.plan(*self.new_products())
        receipt = self.sel_apply(plan, exclude=["502"])
        self.assertEqual({x["pid"] for x in receipt["applied"]}, {"500", "501", "503"})
        self.assertNotIn("502", self.mapped())

    def test_child_without_its_in_plan_master_is_refused(self):
        plan = self.plan(*self.new_products())
        with self.assertRaisesRegex(cs.ApplyRefused, "select 500 too"):
            self.sel_apply(plan, only=["501"])
        with self.assertRaisesRegex(cs.ApplyRefused, "dependency"):
            self.sel_apply(plan, exclude=["500"])
        self.assertEqual(self.fake.posts(), [])
        # a child on an already-mapped master needs nothing else
        receipt = self.sel_apply(plan, only=["503"])
        self.assertEqual([x["pid"] for x in receipt["applied"]], ["503"])

    def test_unknown_hold_and_overlapping_ids_are_refused(self):
        prods, scrapes = self.new_products()
        prods.append(product("505", "NO AMOUNT", False))
        scrapes["505"] = scrape("505", [("100", "")])
        plan = self.plan(prods, scrapes)
        with self.assertRaisesRegex(cs.ApplyRefused, "not decisions in this plan"):
            self.sel_apply(plan, only=["99999"])
        with self.assertRaisesRegex(cs.ApplyRefused, "HOLD"):
            self.sel_apply(plan, only=["505"])
        with self.assertRaisesRegex(cs.ApplyRefused, "both"):
            self.sel_apply(plan, only=["502"], exclude=["502"])
        with self.assertRaisesRegex(cs.ApplyRefused, "manual apply"):
            self.sel_apply(plan, only=["502"], automated=True)
        self.assertEqual(self.fake.posts(), [])

    def test_changed_decision_is_refused(self):
        plan = self.plan(*self.new_products())
        path = self.tmp / "plan" / "plan.json"
        data = json.loads(path.read_text())
        for d in data["decisions"]:
            if d["pid"] == "502":
                d["flags"].append("UNEXPLAINED_STOCK")  # not covered by plan_sha256, covered by decision_sha256
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(cs.ApplyRefused, "502"):
            self.sel_apply(plan, only=["502"])
        self.assertEqual(self.fake.posts(), [])

    def test_expected_decision_shas_must_match(self):
        plan = self.plan(*self.new_products())
        shas = plan["decision_shas"]
        with self.assertRaisesRegex(cs.ApplyRefused, "decision changed"):
            self.sel_apply(plan, only=["502"], expect_decision_shas={"502": "0" * 64})
        with self.assertRaisesRegex(cs.ApplyRefused, "no expected decision sha"):
            self.sel_apply(plan, only=["500", "501"], expect_decision_shas={"500": shas["500"]})
        receipt = self.sel_apply(plan, only=["502"], expect_decision_shas={"502": shas["502"]})
        self.assertEqual([x["pid"] for x in receipt["applied"]], ["502"])

    def test_plan_applied_in_parts_and_retries_are_no_ops(self):
        plan = self.plan(*self.new_products())
        first = self.sel_apply(plan, only=["500"])
        self.assertEqual([x["pid"] for x in first["applied"]], ["500"])
        master_id = first["created"][0]["qbo_id"]
        second = self.sel_apply(plan, only=["501", "502"])  # mapping changed by part 1: accepted for this plan
        self.assertEqual({x["pid"] for x in second["applied"]}, {"501", "502"})
        self.assertEqual({x["pid"]: x["qbo_id"] for x in second["mapping_only"]}["501"], master_id)
        retry = self.sel_apply(plan, only=["502"])
        self.assertEqual(retry["applied"], [])
        self.assertIn("502", retry["already_applied"])
        self.assertEqual(len(self.fake.posts()), 2)
        state = json.loads((self.tmp / "plan" / cs.APPLIED_STATE).read_text())
        self.assertEqual(set(state["applied"]), {"500", "501", "502"})

    def test_cli_accepts_expect_sha_only_and_exclude(self):
        with mock.patch.object(cs, "apply_plan", return_value={"ok": True}) as ap, \
                mock.patch.object(cs, "state_dir", return_value=self.state), \
                mock.patch.object(cs.w7.QBOClient, "for_company_a", return_value=object()):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cs.main(["apply", "--plan-dir", str(self.tmp), "--approval-ref", "R", "--expect-sha", "S",
                              "--only", "500, 501", "--exclude", "9", "--expect-decision-shas", "500=a,501=b",
                              "--mapping", str(self.mapping), "--no-slack", "--json"])
        self.assertEqual(rc, 0)
        kw = ap.call_args.kwargs
        self.assertEqual((kw["expect_sha"], kw["only"], kw["exclude"]), ("S", ["500", "501"], ["9"]))
        self.assertEqual(kw["expect_decision_shas"], {"500": "a", "501": "b"})
        self.assertEqual(json.loads(buf.getvalue()), {"ok": True})


class CatalogueExclusionTests(CatalogueBase):
    def xpath(self):
        return self.mapping.parent / rx.FILE_NAME

    def test_excluded_product_is_not_planned_and_is_reported(self):
        rx.add("product", "600", reason="staff test button", added_by="R", path=self.xpath())
        plan = self.plan([product("600", "TEST BUTTON", False), product("601", "REAL THING", False)],
                         {"600": scrape("600"), "601": scrape("601")})
        self.assertEqual([d["pid"] for d in plan["decisions"]], ["601"])
        self.assertEqual([x["pid"] for x in plan["excluded"]], ["600"])
        self.assertEqual(plan["excluded"][0]["reason"], "staff test button")
        self.assertEqual(plan["counts"]["excluded"], 1)
        self.assertNotIn("600", self.epos.scraped)
        rows = bs.read_csv(self.tmp / "plan" / "review.csv")
        self.assertEqual([r["EPOS Product ID"] for r in rows if r["Status"] == "EXCLUDED"], ["600"])
        self.assertTrue(plan["inputs"]["exclusions"]["sha256"])
        receipt = self.apply(plan)
        self.assertNotIn("600", ProductConversionRegistry.from_csv(self.mapping).by_product_id)
        self.assertEqual([x["pid"] for x in receipt["excluded"]], ["600"])

    def test_child_of_excluded_master_holds_and_master_is_not_pulled_in(self):
        rx.add("product", "610", reason="discontinued", added_by="R", path=self.xpath())
        cat_master = product("610", "OLD CRATE*24", True, vos=24)
        plan = self.plan([cat_master, product("611", "OLD SINGLE", False)],
                         {"611": scrape("611", [("610", "1Each of 24Each")])}, only_ids=["611"])
        d = self.by_pid(plan)["611"]
        self.assertEqual(d["review"], cs.HOLD)
        self.assertTrue(any(r.startswith("MASTER_EXCLUDED(610)") for r in d["reasons"]))
        self.assertNotIn("610", self.epos.scraped)
        self.assertEqual([x["pid"] for x in plan["excluded"]], ["610"])

    def test_exclusion_added_after_the_plan_is_honoured_at_apply(self):
        prods = [product("620", "NEW CRATE*12", True, vos=12), product("621", "NEW SINGLE", False),
                 product("622", "OTHER", False)]
        scrapes = {"620": scrape("620", stock=0), "621": scrape("621", [("620", "1Each of 12Each")]),
                   "622": scrape("622")}
        plan = self.plan(prods, scrapes)
        rx.add("product", "622", reason="not ours", added_by="R", path=self.xpath())
        receipt = self.apply(plan)
        self.assertNotIn("622", {x["pid"] for x in receipt["applied"]})
        self.assertIn("622", {x["pid"] for x in receipt["excluded"]})
        self.assertEqual({x["pid"] for x in receipt["applied"]}, {"620", "621"})
        # excluding a master after the plan refuses an explicit selection of its child
        plan2 = self.plan([product("630", "B CRATE*6", True, vos=6), product("631", "B SINGLE", False)],
                          {"630": scrape("630", stock=0), "631": scrape("631", [("630", "1Each of 6Each")])},
                          name="plan2")
        rx.add("product", "630", reason="x", added_by="R", path=self.xpath())
        with self.assertRaisesRegex(cs.ApplyRefused, "excluded in review_exclusions"):
            cs.apply_plan(self.tmp / "plan2", approval_ref="R", expect_sha=plan2["plan_sha256"], automated=False,
                          client=client(self.fake, writes=True), mapping_path=self.mapping, state=self.state,
                          slack=False, only=["631"])


# ---------------------------------------------------------------- bills: exclusions + Approve=skip
VENDOR_ENV = {vendor_ops.AUTO_ENV: "1", vendor_ops.REF_ENV: "owner vendor yes (test)"}


class BillsExclusionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.xpath = self.f.tmp / rx.FILE_NAME
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in (*VENDOR_ENV, vendor_ops.MAX_ENV, bs.AUTO_ENV, bs.AUTO_REF_ENV):
            os.environ.pop(key, None)

    def scheduled(self, orders, fake):
        import argparse
        from datetime import datetime

        self.f.n += 1
        out = self.f.tmp / f"sched{self.f.n}"
        out.mkdir()
        (out / "po_list.json").write_text(json.dumps({"body": {"orders": [o for o, _ in orders]}}))
        (out / "po_details.jsonl").write_text("".join(json.dumps(d) + "\n" for _, d in orders))
        a = argparse.Namespace(out=str(out), mapping=str(self.f.tmp / "mapping.csv"),
                               vendors=str(self.f.tmp / "vendors.csv"), po_list=str(out / "po_list.json"),
                               po_details=str(out / "po_details.jsonl"), date_from="2026-10-01",
                               date_to="2026-10-05", tax_mode="gross", dup_days=14, dup_min_value="50000",
                               no_slack=True, create_vendors=True)
        with mock.patch.object(bs, "cursor_path", lambda: self.f.cursor), \
                mock.patch.object(bs, "today_lagos", lambda: datetime(2026, 10, 6, 9, 0)):
            out, summary, entries = bs.run_plan(a, client=client(fake), write_client=client(fake, writes=True))
        return out, summary, {e["po"]["ref"]: e for e in entries}

    def test_excluded_supplier_is_never_auto_created_and_its_bill_holds(self):
        rx.add("vendor", "WONUOLA SUPER STORE", reason="owner's own shop", added_by="R", path=self.xpath)
        os.environ.update(VENDOR_ENV)
        fake = VendorFakeQBO()
        _, summary, e = self.scheduled([order(3990, [WATER], supplier="WONUOLA SUPER STORE")], fake)
        self.assertEqual(fake.vendor_posts(), [])
        self.assertEqual(summary["vendor_actions"][0]["state"], vendor_ops.EXCLUDED)
        self.assertEqual(e["3990"]["status"], "HOLD")
        self.assertTrue(any(r.startswith("supplier excluded") for r in e["3990"]["reasons"]))
        self.assertIn("supplier excluded (review_exclusions)", summary["hold_reason_counts"])
        self.assertTrue(summary["exclusions_file"]["sha256"])

    def test_excluded_supplier_holds_even_when_mapped(self):
        rx.add("vendor", "NIGERIAN BOTTLING COMPANY", reason="disputed", added_by="R", path=self.xpath)
        _, _, e, _ = self.f.plan([order(3991, [WATER])])
        self.assertEqual(e["3991"]["status"], "HOLD")
        self.assertTrue(any("supplier excluded" in r for r in e["3991"]["reasons"]))

    def test_excluded_po_is_resolved_outside_the_tool(self):
        rx.add("bill", "EPOS-PO-3992", reason="bookkeeper billed it by hand", added_by="R", path=self.xpath)
        out, summary, e, fake = self.f.plan([order(3992, [WATER]), order(3993, [WATER], received="2026-10-02T11:00:00")])
        self.assertEqual(e["3992"]["status"], "EXCLUDED")
        self.assertIn(bs.PO_EXCLUDED_NOTE, e["3992"]["reasons"][0])
        self.assertEqual([json.loads(x)["po"] for x in (out / "payloads.jsonl").read_text().splitlines()], ["3993"])
        res = self.f.post(out, fake, approve=("3993",))
        self.assertEqual(res["counts"], {"POSTED": 1})
        self.assertEqual(json.loads(self.f.cursor.read_text())["last_complete_business_date"], "2026-10-05")

    def test_approve_skip_is_never_posted_and_counts_as_done(self):
        out, _, _, fake = self.f.plan([order(3994, [WATER]), order(3995, [WATER], received="2026-10-02T12:00:00")],
                                      date_from="2026-10-01", date_to="2026-10-02")
        rows = bs.read_csv(out / "review.csv")
        for r in rows:
            r["Approve"] = {"3994": "yes", "3995": "skip"}[r["PO"]]
        bs.write_csv(out / "review.csv", rows, bs.REVIEW_COLS)
        sha = json.loads((out / "summary.json").read_text())["payloads_sha256"]
        with mock.patch.object(bs, "cursor_path", lambda: self.f.cursor):
            res = bs.run_post(out, client=client(fake, writes=True), registry=self.f.registry(), approval_ref="R",
                              expect_sha=sha, review_path=out / "review.csv")
        self.assertEqual(res["counts"], {"POSTED": 1})
        self.assertEqual([b["DocNumber"] for b in fake.bills.values()], ["EPOS-PO-3994"])
        self.assertEqual(res["cursor"], "2026-10-02")

    def test_exclusions_added_after_the_plan_win_over_approve_yes(self):
        out, _, _, fake = self.f.plan([order(3996, [WATER]),
                                       order(3997, [WATER], supplier="UNCLE SAM'S BAKERY")])
        rx.add("bill", "3996", reason="resolved by hand", added_by="R", path=self.xpath)
        rx.add("vendor", "UNCLE SAM'S BAKERY", reason="stop", added_by="R", path=self.xpath)
        res = self.f.post(out, fake, approve=("3996", "3997"))
        self.assertEqual(fake.posts(), [])
        self.assertEqual(res["counts"], {"EXCLUDED": 1, "HELD_LIVE": 1})
        results = {r["PO"]: r for r in bs.read_csv(out / "results.csv")}
        self.assertEqual(results["3996"]["status"], "RESOLVED")
        self.assertIn("supplier excluded", results["3997"]["detail"])


# ---------------------------------------------------------------- vendors approve
class VendorApproveTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.vendors = self.f.tmp / "vendors.csv"
        self.state = self.f.tmp / "vstate"

    def approve(self, fake, *, writes=False, **kw):
        kw.setdefault("approval_ref", "portal approval 31")
        return vendor_ops.approve(client(fake, writes=writes), vendors_path=self.vendors, state=self.state,
                                  exclusions=rx.load(self.f.tmp / rx.FILE_NAME), **kw)

    def rows(self):
        return {r["EPOS Supplier Name"]: r for r in bs.read_csv(self.vendors)}

    def test_create_dry_run_then_write_with_sha(self):
        fake = VendorFakeQBO()
        dry = self.approve(fake, epos_name="WONUOLA SUPER STORE", create=True, dry_run=True)
        again = self.approve(fake, epos_name="WONUOLA SUPER STORE", create=True, dry_run=True)
        self.assertEqual(dry["result"], "dry_run")
        self.assertEqual(dry["payload_sha256"], again["payload_sha256"])
        self.assertEqual(dry["payload"]["qbo_payload"], {"DisplayName": "WONUOLA SUPER STORE",
                                                         "CompanyName": "WONUOLA SUPER STORE"})
        self.assertEqual(fake.posts(), [])
        with self.assertRaisesRegex(vendor_ops.ApproveRefused, "expect-sha"):
            self.approve(fake, writes=True, epos_name="WONUOLA SUPER STORE", create=True)
        with self.assertRaisesRegex(vendor_ops.ApproveRefused, "expect-sha"):
            self.approve(fake, writes=True, epos_name="WONUOLA SUPER STORE", create=True, expect_sha="0" * 64)
        with self.assertRaisesRegex(vendor_ops.ApproveRefused, "approval-ref"):
            self.approve(fake, writes=True, epos_name="WONUOLA SUPER STORE", create=True,
                         expect_sha=dry["payload_sha256"], approval_ref="")
        self.assertEqual(fake.posts(), [])
        res = self.approve(fake, writes=True, epos_name="WONUOLA SUPER STORE", create=True,
                           expect_sha=dry["payload_sha256"])
        self.assertEqual(res["result"], "created")
        self.assertEqual(len(fake.vendor_posts()), 1)
        self.assertEqual(fake.vendor_posts()[0][2]["requestid"], vendor_ops.vendor_requestid("WONUOLA SUPER STORE"))
        row = self.rows()["WONUOLA SUPER STORE"]
        self.assertEqual((row["QBO Vendor Id"], row["Approved By"]), (res["qbo_vendor_id"], "portal approval 31"))
        receipt = json.loads(Path(res["receipt"]).read_text())
        self.assertEqual(receipt["payload_sha256"], dry["payload_sha256"])
        audit = (self.state / "audit.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(audit[-1])["result"], "created")
        # the held bill now resolves
        _, _, e, _ = self.f.plan([order(3998, [WATER], supplier="WONUOLA SUPER STORE")], fake)
        self.assertEqual(e["3998"]["status"], "READY", e["3998"]["reasons"])
        # re-running the approval (a portal retry) is a no-op
        retry = self.approve(fake, writes=True, epos_name="WONUOLA SUPER STORE", create=True,
                             expect_sha=dry["payload_sha256"])
        self.assertEqual((retry["result"], retry["qbo_vendor_id"]), ("already_approved", res["qbo_vendor_id"]))
        self.assertEqual(len(fake.vendor_posts()), 1)

    def test_create_refused_when_name_used_by_customer(self):
        fake = VendorFakeQBO(customers=[{"Id": "77", "DisplayName": "WONUOLA SUPER STORE"}])
        res = self.approve(fake, epos_name="WONUOLA SUPER STORE", create=True, dry_run=True)
        self.assertEqual(res["result"], "preflight_failed")
        self.assertTrue(any("Customer 77" in p for p in res["problems"]))
        res = self.approve(fake, epos_name="WONUOLA SUPER STORE", create=True, display="WONUOLA STORES IKEJA",
                           dry_run=True)
        self.assertEqual(res["result"], "dry_run")

    def test_link_to_existing_active_vendor(self):
        fake = VendorFakeQBO()
        dry = self.approve(fake, epos_name="NBC PLC", link_to="10", dry_run=True)
        self.assertEqual((dry["result"], dry["payload"]["qbo_vendor_name"]), ("dry_run", "NIGERIAN BOTTLING COMPANY"))
        res = self.approve(fake, writes=True, epos_name="NBC PLC", link_to="10", expect_sha=dry["payload_sha256"])
        self.assertEqual((res["result"], res["qbo_vendor_id"]), ("linked", "10"))
        self.assertEqual(fake.posts(), [])
        self.assertEqual(self.rows()["NBC PLC"]["Approved By"], "portal approval 31")
        self.assertEqual(self.approve(fake, epos_name="NBC PLC", link_to="10", dry_run=True)["result"],
                         "already_approved")

    def test_link_preflight_problems(self):
        fake = VendorFakeQBO(vendors={"10": {"Id": "10", "DisplayName": "NIGERIAN BOTTLING COMPANY", "Active": True},
                                      "12": {"Id": "12", "DisplayName": "OLD CO", "Active": False}})
        self.assertTrue(any("inactive" in p for p in
                            self.approve(fake, epos_name="X LTD", link_to="12", dry_run=True)["problems"]))
        self.assertTrue(any("not readable" in p or "not found" in p for p in
                            self.approve(fake, epos_name="X LTD", link_to="404", dry_run=True)["problems"]))
        res = self.approve(fake, epos_name="NIGERIAN BOTTLING COMPANY", link_to="12", dry_run=True)
        self.assertTrue(any("already maps" in p for p in res["problems"]))
        with self.assertRaises(vendor_ops.ApproveRefused):
            self.approve(fake, epos_name="X LTD", dry_run=True)
        rx.add("vendor", "X LTD", reason="never", added_by="R", path=self.f.tmp / rx.FILE_NAME)
        res = self.approve(fake, epos_name="x  ltd", link_to="10", dry_run=True)
        self.assertTrue(any("excluded" in p for p in res["problems"]))
        self.assertEqual(fake.posts(), [])

    def test_cli_exit_codes(self):
        fake = VendorFakeQBO()

        def run(*argv, writes=False):
            buf = io.StringIO()
            with redirect_stdout(buf), \
                    mock.patch("code_scripts.scripts.akponora_cutover._common.setup_env"), \
                    mock.patch("code_scripts.akponora_ops.common.state_dir", return_value=self.state), \
                    mock.patch.object(cs.w7.QBOClient, "for_company_a",
                                      side_effect=lambda allow_writes=False: client(fake, writes=allow_writes)):
                rc = vendor_ops.main(["approve", "--vendors", str(self.vendors), *argv])
            return rc, json.loads(buf.getvalue())

        rc, dry = run("--epos-name", "ZENITH PLASTICS", "--create", "--approval-ref", "R9", "--dry-run")
        self.assertEqual((rc, dry["result"]), (0, "dry_run"))
        rc, out = run("--epos-name", "ZENITH PLASTICS", "--create", "--approval-ref", "R9", "--expect-sha", "bad")
        self.assertEqual((rc, out["result"]), (4, "refused"))
        rc, out = run("--epos-name", "ZENITH PLASTICS", "--create", "--approval-ref", "R9",
                      "--expect-sha", dry["payload_sha256"])
        self.assertEqual((rc, out["result"]), (0, "created"))
        rc, out = run("--epos-name", "OTHER", "--link-to", "999", "--approval-ref", "R9", "--dry-run")
        self.assertEqual((rc, out["result"]), (2, "preflight_failed"))


if __name__ == "__main__":
    unittest.main()
