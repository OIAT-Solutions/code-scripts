"""catalogue_sync: new EPOS products -> mapping rows / QBO items. All EPOS and QBO access is faked."""
import csv
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from code_scripts.akponora_ops import catalogue_sync as cs
from code_scripts.akponora_ops.common import MAPPING_COLUMNS
from code_scripts.product_conversion import ProductConversionRegistry
from code_scripts.tests.test_akponora_w7_and_journals import FakeQBO, client

DRINKS, GROCERY = "DRINKS & BEVERAGES", "PROVISIONS AND CEREALS"


def product(pid, name, tracked, *, vos=None, cost=100.0, price=200.0, category=DRINKS, tax="VAT"):
    return {"Id": pid, "Name": name, "IsStockTracked": tracked, "VolumeOfSale": vos, "CostPriceExTax": cost,
            "SalePriceIncTax": price, "CostPriceTaxGroupName": tax, "SalePriceTaxGroupName": tax,
            "CategoryName": category, "Barcode": "999", "Sku": None}


def scrape(pid, links=(), stock=None, vol="0"):
    rows = [{"cells": ["", "", f"MASTER {mid}", amount, "", ""], "master_id": mid} for mid, amount in links]
    extra = [] if stock is None else [{"location": "Plot C", "current_stock": str(stock), "current_volume": vol}]
    return {"product_id": pid, "master_rows": rows, "extra": extra}


def map_row(pid, name, typ, target, sku, qbo_id, mult="1", unit="Each"):
    return {"Row ID": f"EPOS-{pid}", "EPOS Product ID": pid, "EPOS Existing SKU": "", "EPOS Name": name,
            "Pipeline Status": "TIER_A", "Review Status": "Approved", "Target QBO Item Type": typ,
            "Target QBO Name": target, "Target QBO SKU": sku, "Target QBO Item Id": qbo_id,
            "Staff Approved Sale Multiplier": mult, "Effective Date": "2026-10-01", "Approved By": "Owner",
            "Canonical Family Key": sku, "Canonical Unit": unit, "Staff Approved Purchase Multiplier": mult}


def live_item(item_id, name, sku, typ="Inventory"):
    it = {"Id": item_id, "Name": name, "FullyQualifiedName": name, "Sku": sku, "Type": typ, "Active": True,
          "SyncToken": "0"}
    if typ == "Inventory":
        it.update(TrackQtyOnHand=True, QtyOnHand=5, AssetAccountRef={"value": "77"}, InvStartDate="2026-10-01")
    return it


class FakeEpos:
    def __init__(self, scrapes, receipts=None):
        self.scrapes, self.receipts = scrapes, receipts or {}
        self.scraped, self.po_calls = [], []

    def scrape(self, ids):
        self.scraped += list(ids)
        return {pid: self.scrapes.get(pid) for pid in ids}

    def po_receipts(self, ids):
        self.po_calls.append(list(ids))
        return self.receipts


BASE_CATALOGUE = [
    product("100", "COKE CAN*24", True, vos=24, cost=4800.0, price=9600.0),
    product("200", "WATER 75CL", True),
    product("300", "EGGS LOOSE", False, category=GROCERY),
]
BASE_MAPPING = [
    map_row("100", "COKE CAN*24", "Inventory", "COKE CAN", "AKP-100", "90001", mult="24",
            unit="Each (1/24 of COKE CAN*24)"),
    map_row("200", "WATER 75CL", "Inventory", "WATER 75CL", "AKP-200", "90002"),
    map_row("300", "EGGS LOOSE", "NonInventory", "EGGS LOOSE", "AKP-NS-300", "90003"),
]
BASE_ITEMS = [live_item("90001", "COKE CAN", "AKP-100"), live_item("90002", "WATER 75CL", "AKP-200"),
              live_item("90003", "EGGS LOOSE", "AKP-NS-300", "NonInventory"),
              live_item("777", "LEGACY \u2014 OLD BISCUIT", "", "Inventory")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.mapping = self.tmp / "mappings" / "approved.csv"
        self.mapping.parent.mkdir()
        with open(self.mapping, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=MAPPING_COLUMNS)
            w.writeheader()
            w.writerows(BASE_MAPPING)
        self.fake = FakeQBO(items=BASE_ITEMS)
        self.slack = []
        patcher = mock.patch.object(cs, "send_slack", side_effect=self.slack.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in (cs.AUTO_ENV, cs.APPROVAL_ENV, cs.CAP_ENV):
            os.environ.pop(key, None)

    def plan(self, new_products, scrapes, receipts=None, name="plan", only_ids=None, qbo=True):
        self.epos = FakeEpos(scrapes, receipts)
        catalogue = [cs.normalize_product(p) for p in BASE_CATALOGUE + new_products]
        return cs.build_plan(out=self.tmp / name, catalogue=catalogue, mapping_path=self.mapping, state=self.state,
                             epos=self.epos, client=client(self.fake) if qbo else None, only_ids=only_ids)

    @staticmethod
    def by_pid(plan):
        return {d["pid"]: d for d in plan["decisions"]}

    def apply(self, plan, *, automated=False, approval="chat yes 2026-10-02", sha=None, cap=None):
        return cs.apply_plan(self.tmp / "plan", approval_ref=approval, expect_sha=plan["plan_sha256"] if sha is None else sha,
                             automated=automated, client=client(self.fake, writes=True), mapping_path=self.mapping,
                             state=self.state, max_creates=cap)


class PlanClassificationTests(Base):
    def test_new_tracked_inventory_payload_qty_zero_asset_77_start_date(self):
        plan = self.plan([product("400", "MALTINA CAN*24", True, vos=24, cost=4800.0, price=9600.0)],
                         {"400": scrape("400", stock=0)})
        d = self.by_pid(plan)["400"]
        self.assertEqual((d["action"], d["review"]), (cs.CREATE_INV, cs.AUTO))
        p = d["payload"]
        self.assertEqual(p["QtyOnHand"], 0)
        self.assertEqual(p["AssetAccountRef"], {"value": "77"})
        self.assertEqual(p["InvStartDate"], "2026-10-01")
        self.assertEqual((p["Sku"], p["Type"], p["Name"]), ("AKP-400", "Inventory", "MALTINA CAN*24"))
        self.assertEqual((p["PurchaseCost"], p["UnitPrice"]), (200.0, 400.0))
        self.assertFalse(p["PurchaseTaxIncluded"])
        self.assertEqual((p["SalesTaxCodeRef"], p["PurchaseTaxCodeRef"]), ({"value": "2"}, {"value": "2"}))
        self.assertEqual((p["IncomeAccountRef"], p["ExpenseAccountRef"]), ({"value": "1150040031"}, {"value": "1150040034"}))
        self.assertEqual((d["multiplier"], d["canonical_unit"]), ("24", "Each (1/24 of MALTINA CAN*24)"))
        self.assertEqual(self.epos.po_calls, [])

    def test_untracked_child_six_each_of_one_each_is_mapping_only_x6(self):
        plan = self.plan([product("401", "WATER 75CL PACK", False)], {"401": scrape("401", [("200", "6Each of 1Each")])})
        d = self.by_pid(plan)["401"]
        self.assertEqual((d["action"], d["review"], d["multiplier"], d["purchase_multiplier"]),
                         (cs.MAPPING_ONLY, cs.AUTO, "6", "6"))
        self.assertEqual((d["target"]["sku"], d["target"]["qbo_id"], d["payload"]), ("AKP-200", "90002", None))

    def test_blank_multiplier_holds_never_defaults_to_one(self):
        plan = self.plan([product("402", "COKE SINGLE", False)], {"402": scrape("402", [("100", "")])})
        d = self.by_pid(plan)["402"]
        self.assertEqual(d["review"], cs.HOLD)
        self.assertEqual(d["multiplier"], "")
        self.assertTrue(any("MULTIPLIER_UNDETERMINED" in r for r in d["reasons"]))
        self.assertEqual(plan["counts"]["hold"], 1)

    def test_trailing_star_n_in_name_is_ignored(self):
        plan = self.plan([product("403", "COKE CAN*12", False), product("404", "SPRITE*12", True)],
                         {"403": scrape("403", [("100", "1Each of 24Each")]), "404": scrape("404", stock=0)})
        ds = self.by_pid(plan)
        self.assertEqual(ds["403"]["multiplier"], "1")
        self.assertEqual(ds["404"]["multiplier"], "1")
        self.assertEqual(ds["404"]["payload"]["PurchaseCost"], 100.0)

    def test_qbo_name_collision_holds(self):
        self.fake.items["900"] = live_item("900", "FANTA 50CL", "", "NonInventory")
        plan = self.plan([product("405", "FANTA 50CL", False)], {"405": scrape("405")})
        d = self.by_pid(plan)["405"]
        self.assertEqual((d["action"], d["review"]), (cs.CREATE_NS, cs.HOLD))
        self.assertTrue(any(r.startswith("QBO_COLLISION") for r in d["reasons"]))

    def test_same_master_twice_amounts_summed(self):
        plan = self.plan([product("406", "COKE TWIN", False)],
                         {"406": scrape("406", [("100", "1Each of 24Each"), ("100", "1Each of 24Each")])})
        d = self.by_pid(plan)["406"]
        self.assertEqual((d["action"], d["multiplier"], d["target"]["sku"]), (cs.MAPPING_ONLY, "2", "AKP-100"))

    def test_untracked_or_archived_master_link_is_noninventory(self):
        plan = self.plan([product("407", "TASTY TOM SACHET", False, category=GROCERY)],
                         {"407": scrape("407", [("300", "1Each of 1Each"), ("99999", "1Each of 10Each")])})
        d = self.by_pid(plan)["407"]
        self.assertEqual((d["action"], d["review"]), (cs.CREATE_NS, cs.AUTO))
        p = d["payload"]
        self.assertEqual((p["Sku"], p["Type"]), ("AKP-NS-407", "NonInventory"))
        self.assertNotIn("QtyOnHand", p)
        self.assertEqual((p["IncomeAccountRef"], p["ExpenseAccountRef"]), ({"value": "1150040024"}, {"value": "74"}))

    def test_unexplained_stock_flagged_and_po_stock_noted(self):
        receipts = {"408": [{"po": "4001", "qty": "3", "status": "Received", "received": "2026-10-02"}]}
        plan = self.plan([product("408", "MILO TIN", True), product("409", "OVALTINE TIN", True)],
                         {"408": scrape("408", stock=3), "409": scrape("409", stock=4)}, receipts)
        ds = self.by_pid(plan)
        self.assertIn("STOCK_VIA_PO", ds["408"]["flags"])
        self.assertEqual(ds["408"]["review"], cs.AUTO)
        self.assertTrue(any("PO#4001" in n for n in ds["408"]["notes"]))
        self.assertIn("UNEXPLAINED_STOCK", ds["409"]["flags"])
        self.assertEqual(ds["409"]["review"], cs.REVIEW)
        self.assertEqual(ds["409"]["payload"]["QtyOnHand"], 0)
        self.assertTrue(cs.needs_attention(plan))

    def test_new_master_and_child_in_same_run(self):
        plan = self.plan([product("410", "PEAK MILK*48", True, vos=48), product("411", "PEAK MILK", False)],
                         {"410": scrape("410", stock=0), "411": scrape("411", [("410", "1Each of 48Each")])})
        ds = self.by_pid(plan)
        self.assertEqual((ds["411"]["action"], ds["411"]["target"]["sku"], ds["411"]["multiplier"]),
                         (cs.MAPPING_ONLY, "AKP-410", "1"))
        self.assertEqual(ds["411"]["family_key"], "AKP-410")

    def test_child_of_held_master_is_held(self):
        plan = self.plan([product("412", "MYSTERY*6", True, category="UNKNOWN"), product("413", "MYSTERY", False)],
                         {"412": scrape("412", stock=0), "413": scrape("413", [("412", "1Each of 1Each")])})
        ds = self.by_pid(plan)
        self.assertEqual(ds["412"]["review"], cs.HOLD)
        self.assertEqual(ds["413"]["review"], cs.HOLD)

    def test_scrape_failure_and_tracked_with_tracked_master_hold(self):
        plan = self.plan([product("414", "A", False), product("415", "B", True)],
                         {"415": scrape("415", [("100", "1Each of 24Each")], stock=0)})
        ds = self.by_pid(plan)
        self.assertEqual((ds["414"]["review"], ds["415"]["review"]), (cs.HOLD, cs.HOLD))

    def test_plan_makes_no_non_get_call_and_writes_evidence(self):
        plan = self.plan([product("416", "NEW JUICE", True)], {"416": scrape("416", stock=0)})
        self.assertEqual(self.fake.posts(), [])
        self.assertTrue(all(c[0] == "GET" for c in self.fake.calls))
        out = self.tmp / "plan"
        for f in ("plan.json", "review.csv", "payloads.jsonl", "proposed_mapping.csv", "summary.json"):
            self.assertTrue((out / f).exists(), f)
        rows = list(csv.DictReader(open(out / "proposed_mapping.csv", encoding="utf-8")))
        new = [r for r in rows if r["EPOS Product ID"] == "416"][0]
        self.assertEqual((new["Pipeline Status"], new["Review Status"], new["Effective Date"], new["Target QBO Item Id"]),
                         ("CATALOGUE_SYNC", "Approved", "2026-10-01", ""))
        self.assertEqual(len(rows), len(BASE_MAPPING) + 1)
        self.assertEqual(json.loads((out / "summary.json").read_text())["plan_sha256"], plan["plan_sha256"])

    def test_changed_and_removed_reported_only(self):
        catalogue_extra = [product("417", "X", True)]
        self.epos = FakeEpos({"417": scrape("417", stock=0)})
        catalogue = [cs.normalize_product(p) for p in [
            product("100", "COKE CAN*24 NEW", True, vos=12), product("300", "EGGS LOOSE", True, category=GROCERY)]
            + catalogue_extra]
        plan = cs.build_plan(out=self.tmp / "plan", catalogue=catalogue, mapping_path=self.mapping, state=self.state,
                             epos=self.epos, client=client(self.fake))
        changed = {c["pid"]: c for c in plan["changed"]}
        self.assertIn("100", changed)
        self.assertTrue(any("name" in x for x in changed["100"]["changes"]))
        self.assertTrue(any("VolumeOfSale" in x for x in changed["100"]["changes"]))
        self.assertTrue(any("stock-tracked" in x for x in changed["300"]["changes"]))
        self.assertEqual([r["pid"] for r in plan["removed"]], ["200"])
        self.assertEqual(self.fake.posts(), [])

    def test_amount_parse_units(self):
        self.assertEqual(cs.parse_amount("500g of 20000g"), (Decimal(500), Decimal(20000)))
        self.assertEqual(cs.parse_amount("1kg of 20000g"), (Decimal(1000), Decimal(20000)))
        self.assertIsNone(cs.parse_amount(""))
        self.assertIsNone(cs.parse_amount("1Each of 1ml"))


class ApplyTests(Base):
    def new_products(self):
        return ([product("500", "PEAK MILK*48", True, vos=48, cost=4800.0, price=9600.0),
                 product("501", "PEAK MILK", False), product("502", "SUYA SPICE", False, category=GROCERY),
                 product("503", "WATER 75CL x6", False)],
                {"500": scrape("500", stock=0), "501": scrape("501", [("500", "1Each of 48Each")]),
                 "502": scrape("502"), "503": scrape("503", [("200", "6Each of 1Each")])})

    def test_apply_refuses_without_approval_or_sha(self):
        plan = self.plan(*self.new_products())
        with self.assertRaises(cs.ApplyRefused):
            self.apply(plan, approval="")
        with self.assertRaises(cs.ApplyRefused):
            self.apply(plan, sha="")
        with self.assertRaises(cs.ApplyRefused):
            self.apply(plan, sha="0" * 64)
        self.assertEqual(self.fake.posts(), [])

    def test_automated_cap_refuses_whole_run(self):
        plan = self.plan(*self.new_products())
        with self.assertRaises(cs.ApplyRefused):
            self.apply(plan, automated=True, cap=1)
        self.assertEqual(self.fake.posts(), [])
        self.assertTrue(any("cap" in m for m in self.slack))

    def test_automated_mode_requires_env(self):
        with self.assertRaises(cs.ApplyRefused):
            cs.run_automated(self.tmp / "auto", mapping_path=self.mapping, state=self.state, catalogue=[],
                             epos=FakeEpos({}), client=client(self.fake))

    def test_apply_creates_verifies_and_installs_mapping_registry_resolves(self):
        plan = self.plan(*self.new_products())
        receipt = self.apply(plan)
        self.assertIsNone(receipt["stopped"])
        posts = self.fake.posts()
        self.assertEqual(len(posts), 2)
        self.assertTrue(all("requestid" in c[2] for c in posts))
        created = {x["sku"]: x["qbo_id"] for x in receipt["created"]}
        self.assertEqual(set(created), {"AKP-500", "AKP-NS-502"})
        self.assertEqual(self.fake.items[created["AKP-500"]]["QtyOnHand"], 0)
        reg = ProductConversionRegistry.from_csv(self.mapping)
        self.assertEqual(reg.source_sha256, receipt["installed"]["sha256"])
        from datetime import date
        rule = reg.resolve(product_name="PEAK MILK", product_id="501", transaction_date=date(2026, 10, 2))
        self.assertEqual((rule.target_qbo_item_id, rule.sale_multiplier), (created["AKP-500"], Decimal(1)))
        self.assertEqual(reg.resolve(product_name="x", product_id="503", transaction_date=date(2026, 10, 2)).sale_multiplier,
                         Decimal(6))
        self.assertEqual(reg.resolve(product_name="x", product_id="502", transaction_date=date(2026, 10, 2)).target_qbo_item_id,
                         created["AKP-NS-502"])
        self.assertTrue((self.mapping.parent / "versions").is_dir())
        self.assertTrue(any("PEAK MILK" in m for m in self.slack))
        self.assertTrue(list((self.state / "receipts").glob("*.json")))
        # second run: nothing new
        again = self.plan([*self.new_products()[0]], self.new_products()[1], name="plan2")
        self.assertEqual(again["counts"]["new"], 0)

    def test_rerun_adopts_item_created_by_previous_run(self):
        plan = self.plan(*self.new_products())
        self.fake.mutate_created = None
        original_install = cs.install

        def boom(*a, **k):
            raise cs.StopRun("disk full")

        with mock.patch.object(cs, "install", side_effect=boom):
            with self.assertRaises(cs.StopRun):
                self.apply(plan)
        self.assertEqual(len(self.fake.posts()), 2)
        self.assertEqual(cs.install, original_install)
        replan = self.plan(*self.new_products(), name="plan")
        adopted = {d["pid"]: d["adopt_qbo_id"] for d in replan["decisions"] if d["adopt_qbo_id"]}
        self.assertEqual(set(adopted), {"500", "502"})
        receipt = self.apply(replan)
        self.assertEqual(len(self.fake.posts()), 2)
        self.assertEqual({x["sku"] for x in receipt["adopted"]}, {"AKP-500", "AKP-NS-502"})
        self.assertTrue(receipt["installed"])

    def test_existing_exact_akp_item_not_created_by_job_is_held(self):
        self.fake.items["950"] = {**live_item("950", "SUYA SPICE", "AKP-NS-502", "NonInventory")}
        plan = self.plan(*self.new_products())
        d = self.by_pid(plan)["502"]
        self.assertEqual(d["review"], cs.HOLD)
        self.assertTrue(any("QBO_EXISTING_ITEM" in r for r in d["reasons"]))

    def test_manual_apply_skips_holds_and_applies_review_rows(self):
        prods, scrapes = self.new_products()
        prods.append(product("504", "UNKNOWN STOCK", True))
        scrapes["504"] = scrape("504", stock=7)
        prods.append(product("505", "NO AMOUNT", False))
        scrapes["505"] = scrape("505", [("100", "")])
        plan = self.plan(prods, scrapes)
        ds = self.by_pid(plan)
        self.assertEqual((ds["504"]["review"], ds["505"]["review"]), (cs.REVIEW, cs.HOLD))
        receipt = self.apply(plan)
        reg = ProductConversionRegistry.from_csv(self.mapping)
        self.assertIn("504", reg.by_product_id)
        self.assertNotIn("505", reg.by_product_id)
        self.assertEqual([x["pid"] for x in receipt["unexplained_stock"]], ["504"])
        self.assertTrue(all(self.fake.items[x["qbo_id"]].get("QtyOnHand", 0) == 0 for x in receipt["created"]))

    def test_automated_apply_skips_review_rows(self):
        prods, scrapes = self.new_products()
        prods.append(product("504", "UNKNOWN STOCK", True))
        scrapes["504"] = scrape("504", stock=7)
        plan = self.plan(prods, scrapes)
        receipt = self.apply(plan, automated=True, cap=25)
        reg = ProductConversionRegistry.from_csv(self.mapping)
        self.assertNotIn("504", reg.by_product_id)
        self.assertIn("501", reg.by_product_id)
        self.assertEqual([x["pid"] for x in receipt["skipped_review"]], ["504"])

    def test_verification_failure_stops_before_install(self):
        plan = self.plan(*self.new_products())
        self.fake.mutate_created = {"QtyOnHand": 10}
        with self.assertRaises(cs.StopRun):
            self.apply(plan)
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(len(ProductConversionRegistry.from_csv(self.mapping).rules), len(BASE_MAPPING))

    def test_mapping_changed_since_plan_refuses(self):
        plan = self.plan(*self.new_products())
        with open(self.mapping, "a", encoding="utf-8") as fh:
            fh.write("\n")
        with self.assertRaises(cs.ApplyRefused):
            self.apply(plan)


class EnsureProductsMappedTests(Base):
    def run_ensure(self, ids, prods, scrapes):
        catalogue = [cs.normalize_product(p) for p in BASE_CATALOGUE + prods]
        with mock.patch.object(cs.w7.QBOClient, "for_company_a",
                               side_effect=lambda allow_writes=False, **k: client(self.fake, writes=allow_writes)):
            return cs.ensure_products_mapped(ids, out_dir=self.tmp / "ensure", mapping_path=self.mapping,
                                             state=self.state, catalogue=catalogue, epos=FakeEpos(scrapes))

    def test_nothing_unmapped_is_a_no_op(self):
        result = self.run_ensure({"100", "300"}, [], {})
        self.assertEqual((result["unmapped"], result["unresolved"]), ([], []))
        self.assertEqual(self.fake.calls, [])

    def test_plan_only_without_automation(self):
        result = self.run_ensure({"100", "600"}, [product("600", "NEW TEA", True)], {"600": scrape("600", stock=0)})
        self.assertEqual((result["unmapped"], result["unresolved"], result["applied"]), (["600"], ["600"], False))
        self.assertEqual(self.fake.posts(), [])
        self.assertTrue(self.slack)

    def test_automated_resolves_and_pulls_in_unmapped_master(self):
        os.environ.update({cs.AUTO_ENV: "1", cs.APPROVAL_ENV: "owner chat 2026-10-02 auto catalogue"})
        prods = [product("601", "NEW TEA*10", True, vos=10), product("602", "NEW TEA", False),
                 product("603", "GONE", False)]
        scrapes = {"601": scrape("601", stock=0), "602": scrape("602", [("601", "1Each of 10Each")])}
        result = self.run_ensure({"602", "999"}, prods, scrapes)
        self.assertEqual(result["unmapped"], ["602", "999"])
        self.assertEqual(result["unresolved"], ["999"])
        self.assertTrue(result["applied"])
        self.assertIn("999", result["holds"])
        reg = ProductConversionRegistry.from_csv(self.mapping)
        self.assertIn("601", reg.by_product_id)


if __name__ == "__main__":
    unittest.main()
