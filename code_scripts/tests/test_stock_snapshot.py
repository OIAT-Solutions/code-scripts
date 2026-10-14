"""stock_snapshot: EPOS stock vs QBO QtyOnHand. Everything faked: no EPOS, no QBO, no Slack."""
from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from code_scripts.akponora_ops import stock_snapshot as ss

MAP_COLS = ["Row ID", "EPOS Product ID", "EPOS Name", "Review Status", "Target QBO Item Type", "Target QBO Name",
            "Target QBO SKU", "Target QBO Item Id", "Staff Approved Sale Multiplier", "Canonical Family Key",
            "Canonical Unit"]


def mrow(pid, name, sku, iid, typ="Inventory", mult="1", unit="CAN"):
    return {"Row ID": pid, "EPOS Product ID": pid, "EPOS Name": name, "Review Status": "approved",
            "Target QBO Item Type": typ, "Target QBO Name": name, "Target QBO SKU": sku, "Target QBO Item Id": iid,
            "Staff Approved Sale Multiplier": mult, "Canonical Family Key": sku, "Canonical Unit": unit}


MAPPING = [
    # COKE family: master 100 (VolumeOfSale 12, sells cans), child 101 = pack of 12 (also stock-tracked)
    mrow("100", "COKE 35CL", "AKP-100", "5001"),
    mrow("101", "COKE 35CL X12", "AKP-100", "5001", mult="12"),
    mrow("200", "WATER 75CL", "AKP-200", "5002"),           # no VolumeOfSale: TotalStock
    mrow("300", "FANTA 50CL", "AKP-300", "5003"),           # matches within tolerance
    mrow("400", "BREAD", "AKP-400", "5004"),                # negative in QBO
    mrow("500", "SUGAR 1KG", "AKP-500", "5005"),            # negative in EPOS
    mrow("600", "MILO 400G", "AKP-600", "5006"),            # ambiguous name in EPOS
    mrow("700", "SALT 250G", "AKP-700", "5007"),            # tracked, no stock row
    mrow("800", "OLD RICE", "AKP-800", "5008"),             # master no longer tracked
    mrow("900", "CARRIER BAG", "AKP-NS-900", "5009", typ="NonInventory", unit="EACH"),
    mrow("950", "GHOST ITEM", "AKP-950", "5999"),           # QBO item missing
]

CATALOGUE = [
    {"Id": 100, "Name": "COKE 35CL", "IsStockTracked": True, "VolumeOfSale": 12},
    {"Id": 101, "Name": "COKE 35CL X12", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 200, "Name": "WATER 75CL", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 300, "Name": "FANTA 50CL", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 400, "Name": "BREAD", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 500, "Name": "SUGAR 1KG", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 600, "Name": "MILO 400G", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 601, "Name": "milo 400g", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 700, "Name": "SALT 250G", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 800, "Name": "OLD RICE", "IsStockTracked": False, "VolumeOfSale": None},
    {"Id": 900, "Name": "CARRIER BAG", "IsStockTracked": False, "VolumeOfSale": None},
    {"Id": 950, "Name": "GHOST ITEM", "IsStockTracked": True, "VolumeOfSale": None},
    {"Id": 1000, "Name": "NEW CHOCOLATE", "IsStockTracked": True, "VolumeOfSale": None},  # unmapped, tracked
    {"Id": 1001, "Name": "GIFT WRAP", "IsStockTracked": False, "VolumeOfSale": None},     # unmapped, untracked
]


def srow(name, full, loose, total):
    return {"Name": name, "Barcode": "", "MeasuredCurrentStock": str(full), "CurrentVolume": str(loose),
            "TotalStock": str(total), "MeasuredCostPrice": "0", "CurrentVolume ": "", "TotalCost": "0"}


STOCK = [
    srow("COKE 35CL", 3, 5, "3.4167"),        # 3 x 12 + 5 = 41 cans
    srow("COKE 35CL X12", 3, 0, 3),           # the child's own row: must NOT be added
    srow("WATER 75CL", 20, 0, 20),
    srow("FANTA 50CL", 7, 0, "7.0004"),
    srow("BREAD", 5, 0, 5),
    srow("SUGAR 1KG", -2, 0, -2),
    srow("MILO 400G", 4, 0, 4),
    srow("GHOST ITEM", 1, 0, 1),
    srow("NEW CHOCOLATE", 9, 0, 9),
    srow("UNKNOWN THING", 1, 0, 1),
    {"Name": "Total:", "TotalStock": "999"},
]


def qitem(iid, sku, name, qty, typ="Inventory", active=True):
    return {"Id": iid, "Sku": sku, "Name": name, "QtyOnHand": qty, "Active": active, "Type": typ,
            "UnitPrice": 1}


QBO_ITEMS = [
    qitem("5001", "AKP-100", "COKE 35CL", 41),
    qitem("5002", "AKP-200", "WATER 75CL", 18),
    qitem("5003", "AKP-300", "FANTA 50CL", 7),
    qitem("5004", "AKP-400", "BREAD", -3),
    qitem("5005", "AKP-500", "SUGAR 1KG", 0),
    qitem("5006", "AKP-600", "MILO 400G", 4),
    qitem("5007", "AKP-700", "SALT 250G", 2),
    qitem("5008", "AKP-800", "OLD RICE", 1),
    {"Id": "5009", "Sku": "AKP-NS-900", "Name": "CARRIER BAG", "Active": True, "Type": "NonInventory"},
    qitem("5100", "AKP-1100", "CREATED BY HAND", 0),
    qitem("77", "", "LEGACY — COKE", 10),
]


class FakeClient:
    def __init__(self, items, inactive=()):
        self.items, self.inactive, self.queries = list(items), list(inactive), []

    def query_all(self, sql, entity):
        self.queries.append(sql)
        assert sql.lower().startswith("select"), sql
        return list(self.inactive) if "Active = false" in sql else list(self.items)


def build(**kw):
    args = dict(mapping_rows=MAPPING, catalogue=ss.normalize_catalogue(CATALOGUE), stock_rows=STOCK,
                qbo_items=ss.relevant_qbo_items(QBO_ITEMS, MAPPING), tolerance=Decimal("0.001"))
    args.update(kw)
    return ss.build_snapshot(**args)


def by_sku(snap):
    return {r["family_sku"]: r for r in snap["rows"]}


class BuildSnapshotTests(unittest.TestCase):
    def test_canonical_conversion_uses_master_only(self):
        r = by_sku(build())["AKP-100"]
        self.assertEqual(r["epos_qty_canonical"], 41)  # 3 x 12 + 5, child row (3 packs) not added
        self.assertEqual(r["qbo_qty_on_hand"], 41)
        self.assertEqual(r["status"], ss.MATCH)
        self.assertEqual(r["epos_master_id"], "100")
        self.assertEqual(r["epos_volume_of_sale"], 12)
        self.assertEqual(r["epos_product_ids"], ["100", "101"])
        self.assertEqual(r["canonical_unit"], "CAN")

    def test_no_volume_of_sale_uses_total_stock(self):
        r = by_sku(build())["AKP-200"]
        self.assertEqual((r["epos_qty_canonical"], r["qbo_qty_on_hand"], r["difference"]), (20, 18, 2))
        self.assertEqual(r["status"], ss.DIFFERENT)
        self.assertFalse(r["likely_timing"])

    def test_statuses(self):
        rows = by_sku(build())
        expect = {"AKP-300": ss.MATCH, "AKP-400": ss.NEGATIVE_QBO, "AKP-500": ss.NEGATIVE_EPOS,
                  "AKP-600": ss.NOT_IN_EPOS_REPORT, "AKP-700": ss.NOT_IN_EPOS_REPORT,
                  "AKP-800": ss.NOT_TRACKED_IN_EPOS, "AKP-NS-900": ss.NOT_TRACKED_IN_EPOS,
                  "AKP-950": ss.NO_QBO_ITEM}
        self.assertEqual({k: rows[k]["status"] for k in expect}, expect)
        self.assertEqual(rows["AKP-300"]["difference"], 0.0004)  # within 0.001
        self.assertEqual(rows["AKP-600"]["flags"], ["AMBIGUOUS_EPOS_NAME"])
        self.assertIsNone(rows["AKP-700"]["epos_qty_canonical"])
        self.assertIsNone(rows["AKP-950"]["qbo_qty_on_hand"])
        self.assertEqual(rows["AKP-950"]["qbo_item_id"], "5999")

    def test_tolerance_is_configurable(self):
        r = by_sku(build(tolerance=Decimal("0.0001")))["AKP-300"]
        self.assertEqual(r["status"], ss.DIFFERENT)
        self.assertEqual(r["tolerance"], 0.0001)

    def test_noninventory_row(self):
        r = by_sku(build())["AKP-NS-900"]
        self.assertEqual(r["type"], "NonInventory")
        self.assertIsNone(r["qbo_qty_on_hand"])
        self.assertIsNone(r["epos_qty_canonical"])
        self.assertEqual(r["canonical_unit"], "EACH")

    def test_unmapped_and_unassigned_lists(self):
        snap = build()
        unmapped = {u["epos_product_id"]: u for u in snap["unmapped_epos_products"]}
        self.assertEqual(set(unmapped), {"601", "1000", "1001"})
        self.assertTrue(unmapped["1000"]["tracked"])
        self.assertEqual(unmapped["1000"]["epos_qty"], 9)
        self.assertFalse(unmapped["1001"]["tracked"])
        reasons = {u["Name"]: u["reason"] for u in snap["unassigned_stock_rows"]}
        self.assertEqual(reasons["MILO 400G"], "AMBIGUOUS_TRACKED_NAME")
        self.assertEqual(reasons["UNKNOWN THING"], "NOT_IN_CATALOGUE")
        self.assertNotIn("Total:", reasons)
        extra = [x["sku"] for x in snap["qbo_akp_items_not_in_mapping"]]
        self.assertEqual(extra, ["AKP-1100"])  # legacy items are not reported

    def test_summary_counts_and_text(self):
        snap = build()
        b = snap["summary"]["by_status"]
        self.assertEqual(set(b), set(ss.STATUSES))
        self.assertEqual((b["MATCH"], b["DIFFERENT"], b["NEGATIVE_QBO"]), (2, 1, 1))
        self.assertEqual(snap["summary"]["rows"], len(MAPPING) - 1)  # one row per family (COKE has 2 products)
        self.assertEqual(snap["summary"]["unmapped_tracked"], 2)
        text = ss.summary_text(snap)
        self.assertTrue(text.startswith("Stock check: 2 match, 1 different, 1 negative in QuickBooks"))
        self.assertIn("1 negative in EPOS", text)
        big = {"summary": {"by_status": {"MATCH": 3812, "DIFFERENT": 41, "NEGATIVE_QBO": 11}}}
        self.assertEqual(ss.summary_text(big), "Stock check: 3,812 match, 41 different, 11 negative in QuickBooks")

    def test_likely_timing_from_sales_and_receipts(self):
        snap = build(sold_ids={"200"}, context={"last_posted_sales_date": "2026-10-02"})
        r = by_sku(snap)["AKP-200"]
        self.assertTrue(r["likely_timing"])
        self.assertIn("2026-10-02", r["timing_reasons"][0])
        snap = build(received={"200": ["received 2026-10-03 on EPOS PO 4001 (bill HOLD)"]})
        self.assertTrue(by_sku(snap)["AKP-200"]["likely_timing"])
        self.assertEqual(snap["summary"]["different_likely_timing"], 1)
        self.assertIsNone(by_sku(snap)["AKP-300"]["likely_timing"])  # only DIFFERENT rows carry the flag

    def test_inactive_qbo_item_flagged(self):
        items = [dict(i, Active=False) if i.get("Id") == "5003" else i for i in QBO_ITEMS]
        r = by_sku(build(qbo_items=ss.relevant_qbo_items(items, MAPPING)))["AKP-300"]
        self.assertIn("QBO_ITEM_INACTIVE", r["flags"])

    def test_catalogue_snapshot_dict_format(self):
        cat = ss.normalize_catalogue({"100": {"Name": "COKE 35CL", "IsStockTracked": True, "VolumeOfSale": "12"}})
        self.assertEqual(cat["100"]["VolumeOfSale"], 12)
        with self.assertRaises(ss.SnapshotError):
            ss.normalize_catalogue([{"Id": 1, "Name": "A"}, {"Id": "1", "Name": "B"}])


class ContextTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_last_posted_from_daily_summaries_and_uploaded(self):
        daily, up = self.root / "daily", self.root / "Uploaded"
        for day, mode, status, dry in (("2026-10-01", "post", "ok", False), ("2026-10-02", "dry-run", "review", False),
                                       ("2026-10-03", "post", "ok", True)):
            (daily / day).mkdir(parents=True)
            (daily / day / "summary.json").write_text(json.dumps({
                "business_date": day, "dry_run": dry,
                "steps": [{"name": "sales", "status": status, "counts": {"mode": mode}}]}))
        self.assertEqual(ss.last_posted_sales_date(daily, up, "meta.json"), "2026-10-01")
        (up / "2026-10-02").mkdir(parents=True)
        (up / "2026-10-02" / "meta.json").write_text(json.dumps({"target_date": "2026-10-02",
                                                                 "upload_stats": {"uploaded": 6}}))
        self.assertEqual(ss.last_posted_sales_date(daily, up, "meta.json"), "2026-10-02")
        self.assertIsNone(ss.last_posted_sales_date(None, None, "meta.json"))

    def test_business_context_lists_unposted_days(self):
        from datetime import date

        ctx = ss.business_context(date(2026, 10, 3), "2026-10-01")
        self.assertEqual(ctx["unposted_sales_days"], ["2026-10-02", "2026-10-03"])
        self.assertIn("2026-10-01", ctx["note"])
        tz = ZoneInfo("Africa/Lagos")
        self.assertEqual(ss.current_business_date(datetime(2026, 10, 3, 4, 59, tzinfo=tz)).isoformat(), "2026-10-02")

    def test_sold_ids_from_archived_bookkeeping(self):
        folder = self.root / "Uploaded" / "2026-10-02"
        folder.mkdir(parents=True)
        with open(folder / "BookKeeping_NORA_2026-10-02.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["ProductId", "Product"])
            w.writeheader()
            w.writerows([{"ProductId": "200.0", "Product": "WATER"}, {"ProductId": "300", "Product": "FANTA"}])
        ids, path = ss.sold_product_ids(self.root / "Uploaded", "2026-10-02", "meta.json")
        self.assertEqual(ids, {"200", "300"})
        self.assertTrue(path.endswith(".csv"))
        self.assertEqual(ss.sold_product_ids(self.root / "Uploaded", "2026-09-30", "meta.json"), (set(), None))

    def test_recent_receipts_from_bills_review(self):
        day = self.root / "daily" / "2026-10-03"
        bills = day / "run_x" / "bills"
        bills.mkdir(parents=True)
        (day / "summary.json").write_text(json.dumps({"steps": [{"name": "bills", "out": str(bills)}]}))
        with open(bills / "review.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["PO", "Received Date", "Status"])
            w.writeheader()
            w.writerows([{"PO": "1", "Received Date": "2026-10-02", "Status": "READY"},
                         {"PO": "2", "Received Date": "2026-09-28", "Status": "HOLD"},
                         {"PO": "3", "Received Date": "2026-09-28", "Status": "SKIP"}])
        with open(bills / "review_lines.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["PO", "EPOS Product ID"])
            w.writeheader()
            w.writerows([{"PO": "1", "EPOS Product ID": "200"}, {"PO": "2", "EPOS Product ID": "300"},
                         {"PO": "3", "EPOS Product ID": "400"}])
        found, path = ss.recent_receipts(self.root / "daily", "2026-10-02")
        self.assertEqual(set(found), {"200", "300"})
        self.assertIn("bill HOLD", found["300"][0])


class RunTests(unittest.TestCase):
    """run() / main(): files, partial refresh, locking, failure handling."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dir = self.root / "stock_snapshot"
        self.mapping = self.root / "approved.csv"
        with open(self.mapping, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=MAP_COLS)
            w.writeheader()
            w.writerows(MAPPING)
        self.catalogue = self.root / "catalogue_snapshot.json"
        self.catalogue.write_text(json.dumps({str(p["Id"]): {k: v for k, v in p.items() if k != "Id"}
                                              for p in CATALOGUE}))
        self.stock = self.write_stock("stock1.csv", STOCK)
        patches = {"mapping_file": lambda: self.mapping, "_daily_root": lambda: self.root / "daily",
                   "_uploaded_dir": lambda: self.root / "Uploaded", "_metadata_name": lambda: "meta.json",
                   "snapshot_dir": lambda: self.dir, "default_catalogue_path": lambda: self.catalogue}
        for name, fn in patches.items():
            p = mock.patch.object(ss, name, fn)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {ss.TOLERANCE_ENV: ""})
        env.start()
        self.addCleanup(env.stop)

    def write_stock(self, name, rows):
        path = self.root / name
        cols = list(ss.STOCK_COLUMNS)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return path

    def latest(self):
        return json.loads((self.dir / "latest.json").read_text())

    def test_full_run_writes_latest_and_dated_copy(self):
        client = FakeClient(QBO_ITEMS)
        downloaded = []

        def downloader(out):
            downloaded.append(out)
            return self.stock

        snap = ss.run(client=client, downloader=downloader, directory=self.dir,
                      now=datetime(2026, 10, 3, 9, 0, tzinfo=ZoneInfo("Africa/Lagos")))
        self.assertEqual(len(downloaded), 1)
        self.assertTrue(all(q.lower().startswith("select") for q in client.queries))
        latest = self.latest()
        self.assertEqual(latest["summary"], snap["summary"])
        self.assertTrue((self.dir / "history" / "stock_snapshot_2026-10-03.json").exists())
        for key in ("generated_at", "sources", "business_context", "rows", "unmapped_epos_products", "summary",
                    "summary_text", "tolerance", "schema_version"):
            self.assertIn(key, latest)
        self.assertEqual(set(latest["sources"]), {"epos_stock_report", "catalogue", "qbo", "mapping"})
        self.assertEqual(latest["business_context"]["current_business_date"], "2026-10-03")
        self.assertEqual(by_sku(latest)["AKP-100"]["status"], ss.MATCH)

    def test_partial_refresh_keeps_the_other_side(self):
        ss.run(client=FakeClient(QBO_ITEMS), stock_report=self.stock, directory=self.dir)
        first = self.latest()
        # --no-qbo: new EPOS stock, QBO side reused (client never called)
        stock2 = self.write_stock("stock2.csv", [srow("WATER 75CL", 18, 0, 18) if r.get("Name") == "WATER 75CL" else r
                                                 for r in STOCK])
        ss.run(use_qbo=False, client=mock.Mock(side_effect=AssertionError), stock_report=stock2, directory=self.dir)
        second = self.latest()
        self.assertEqual(by_sku(second)["AKP-200"]["status"], ss.MATCH)
        self.assertEqual(second["sources"]["qbo"]["read_at"], first["sources"]["qbo"]["read_at"])
        self.assertFalse(second["sources"]["qbo"]["refreshed_this_run"])
        # --no-epos: new QBO quantities, EPOS side reused (no download)
        items = [dict(i, QtyOnHand=25) if i.get("Id") == "5002" else i for i in QBO_ITEMS]
        ss.run(use_epos=False, client=FakeClient(items), downloader=mock.Mock(side_effect=AssertionError),
               directory=self.dir)
        third = self.latest()
        r = by_sku(third)["AKP-200"]
        self.assertEqual((r["epos_qty_canonical"], r["qbo_qty_on_hand"], r["status"]), (18, 25, ss.DIFFERENT))
        self.assertEqual(third["sources"]["epos_stock_report"]["path"], str(stock2))

    def test_partial_refresh_without_cache_fails(self):
        with self.assertRaises(ss.SnapshotError):
            ss.run(use_epos=False, client=FakeClient(QBO_ITEMS), directory=self.dir)

    def test_main_failure_keeps_previous_latest(self):
        rc = ss.main(["run", "--stock-report", str(self.stock)], client=FakeClient(QBO_ITEMS))
        self.assertEqual(rc, 0)
        before = (self.dir / "latest.json").read_text()
        out = self.root / "ev"
        bad = self.write_stock("bad.csv", [])
        bad.write_text("Foo,Bar\n1,2\n")
        rc = ss.main(["run", "--stock-report", str(bad), "--out", str(out)], client=FakeClient(QBO_ITEMS))
        self.assertEqual(rc, ss.EXIT_FAILED)
        self.assertEqual((self.dir / "latest.json").read_text(), before)
        self.assertIn("StockReport", json.loads((out / "summary.json").read_text())["error"])

    def test_main_writes_evidence_summary_and_optional_slack(self):
        out, sent = self.root / "ev", []
        rc = ss.main(["run", "--stock-report", str(self.stock), "--out", str(out), "--slack", "--tolerance", "0.01"],
                     client=FakeClient(QBO_ITEMS), slack=sent.append)
        self.assertEqual(rc, 0)
        summary = json.loads((out / "summary.json").read_text())
        self.assertTrue(summary["summary_text"].startswith("Stock check:"))
        self.assertEqual(sent, [summary["summary_text"]])
        self.assertEqual(self.latest()["tolerance"], 0.01)

    def test_main_busy_lock(self):
        holder = ss.locked(self.dir)
        try:
            self.assertEqual(ss.main(["run", "--stock-report", str(self.stock)], client=FakeClient(QBO_ITEMS)),
                             ss.EXIT_BUSY)
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()
