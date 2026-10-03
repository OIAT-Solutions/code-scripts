import copy
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from code_scripts.akponora_ops import item_guard as guard_mod
from code_scripts.akponora_ops.common import LEGACY_PREFIX, MAPPING_COLUMNS, write_csv
from code_scripts.scripts.akponora_cutover._common import ReadOnlyQBO

SINCE = "2026-10-01T00:00:00+01:00"
OLD = {"CreateTime": "2026-01-10T10:00:00-07:00", "LastUpdatedTime": "2026-01-10T10:00:00-07:00"}
NEW = {"CreateTime": "2026-10-02T05:00:00-07:00", "LastUpdatedTime": "2026-10-02T05:00:00-07:00",
       "LastModifiedByRef": {"value": "9130", "name": "Bookkeeper"}}
UPDATED = {"CreateTime": "2026-01-10T10:00:00-07:00", "LastUpdatedTime": "2026-10-02T05:00:00-07:00"}


def akp_item(item_id, sku, name, meta=NEW, item_type="Inventory", asset="77", qty=0, **extra):
    item = {"Id": item_id, "Sku": sku, "Name": name, "Type": item_type, "Active": True, "MetaData": meta,
            "QtyOnHand": qty, "PurchaseCost": 100}
    if item_type == "Inventory":
        item.update({"AssetAccountRef": {"value": asset}, "InvStartDate": "2026-10-01"})
    item.update(extra)
    return item


BASE_ITEMS = [
    akp_item("15032", "AKP-1596315", "SNIPER-FLYING INSECT KILLER600ml"),
    akp_item("15033", "AKP-NS-1596400", "LOOSE RICE KG", item_type="NonInventory"),
    akp_item("15034", "AKP-1596317", "COCA COLA 50CL", meta=OLD, qty=-3),
    akp_item("15030", "AKP-UNMAPPED-EPOS-SALES", "AKP-UNMAPPED-EPOS-SALES", meta=OLD, item_type="NonInventory"),
    {"Id": "900", "Name": LEGACY_PREFIX + "COCA COLA 50CL", "Sku": "OLD-1", "Type": "Inventory", "Active": True,
     "QtyOnHand": 12, "MetaData": OLD, "AssetAccountRef": {"value": "1150040011"}},
]
BASE_ACCOUNTS = [
    {"Id": "77", "Name": "Inventory Asset", "FullyQualifiedName": "Inventory Asset", "CurrentBalance": 1000},
    {"Id": "87", "Name": "210200 - Goods Received Not Invoiced", "CurrentBalance": -50},
    {"Id": "1150040008", "Name": "120000 - Inventory", "FullyQualifiedName": "120000 - Inventory", "CurrentBalance": 0},
    {"Id": "1150040011", "Name": "120100 - Grocery", "FullyQualifiedName": "120000 - Inventory:120100 - Grocery",
     "CurrentBalance": 0},
    {"Id": "200", "Name": "500000 - Cost of Sales", "CurrentBalance": 0},
]


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class GetOnlyTransport:
    """Stands in for the ``requests`` module: only ``get`` exists."""

    def __init__(self, data):
        self.data = data
        self.queries = []

    def get(self, url, headers=None, timeout=None):
        query = parse_qs(urlparse(url).query)["query"][0]
        self.queries.append(query)
        entity = re.search(r"from (\w+)", query).group(1)
        rows = self.data.get(entity, [])
        if entity == "Item":
            want_active = "Active = false" not in query
            rows = [r for r in rows if r.get("Active", True) == want_active]
        start = int(re.search(r"startposition (\d+)", query).group(1))
        size = int(re.search(r"maxresults (\d+)", query).group(1))
        return FakeResponse({"QueryResponse": {entity: copy.deepcopy(rows[start - 1:start - 1 + size])}})

    def __getattr__(self, name):
        raise AssertionError(f"non-GET HTTP call attempted: requests.{name}")


def fake_client(data):
    client = ReadOnlyQBO.__new__(ReadOnlyQBO)
    client._requests = GetOnlyTransport(data)
    client._get_token = lambda: "token"
    client._token = "token"
    client.base = "https://example.invalid/v3/company/1"
    client.minorversion = "75"
    client._min_interval = 0.0
    client._last = 0.0
    client.requests = 0
    return client


def mapping_row(item_id, sku, name, item_type="Inventory", product_id=None):
    product_id = product_id or sku.rsplit("-", 1)[-1]
    return {
        "Row ID": f"EPOS-{product_id}", "EPOS Product ID": product_id, "EPOS Name": name, "Pipeline Status": "TIER_A",
        "Review Status": "Approved", "Target QBO Item Type": item_type, "Target QBO Name": name,
        "Target QBO SKU": sku, "Target QBO Item Id": item_id, "Staff Approved Sale Multiplier": "1",
        "Effective Date": "2026-10-01", "Approved By": "test", "Canonical Family Key": sku,
        "Canonical Unit": "Each", "Staff Approved Purchase Multiplier": "1",
    }


MAPPING = [
    mapping_row("15032", "AKP-1596315", "SNIPER-FLYING INSECT KILLER600ml"),
    mapping_row("15033", "AKP-NS-1596400", "LOOSE RICE KG", item_type="NonInventory"),
    mapping_row("15034", "AKP-1596317", "COCA COLA 50CL"),
]


def bill(bill_id, lines, txn_date="2026-10-02", meta=NEW, note=""):
    return {"Id": bill_id, "DocNumber": f"PO-{bill_id}", "TxnDate": txn_date, "VendorRef": {"value": "5", "name": "GPFH"},
            "PrivateNote": note, "MetaData": meta, "TotalAmt": sum(l["Amount"] for l in lines), "Line": lines}


def item_line(item_id, amount=500.0, detail="ItemBasedExpenseLineDetail"):
    return {"Amount": amount, "DetailType": detail, detail: {"ItemRef": {"value": item_id}}}


def account_line(account_id, amount=700.0):
    return {"Amount": amount, "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": account_id}}}


class ItemGuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mapping = self.root / "approved.csv"
        write_csv(self.mapping, MAPPING, MAPPING_COLUMNS)
        self.state = self.root / "state" / "item_guard"
        self.state.mkdir(parents=True)
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def data(self, items=(), accounts=None, **entities):
        return {"Item": [*copy.deepcopy(BASE_ITEMS), *items], "Account": accounts or copy.deepcopy(BASE_ACCOUNTS),
                **entities}

    def run_guard(self, data, argv=(), client=None):
        self.client = client or fake_client(data)
        with redirect_stdout(io.StringIO()):
            code = guard_mod.main(
                ["--since", SINCE, "--out", str(self.root / "out"), *argv],
                client=self.client, state=self.state, mapping_path=self.mapping,
                catalogue_dir=self.root / "catalogue_sync", slack=self.sent.append,
                now=lambda: datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc),
            )
        self.report = json.loads((self.root / "out" / "report.json").read_text())
        return code

    def checks(self, severity=guard_mod.ALERT):
        return [f["check"] for f in self.report["findings"] if f["severity"] == severity]

    def finding(self, check):
        return next(f for f in self.report["findings"] if f["check"] == check)


class ItemChecksTest(ItemGuardTestCase):
    def test_non_akp_item_created_is_alert(self):
        self.run_guard(self.data(items=[{"Id": "20001", "Name": "Peak Milk Tin", "Sku": "", "Type": "Inventory",
                                         "Active": True, "MetaData": NEW, "QtyOnHand": 0}]))
        self.assertIn("non_akp_item_created", self.checks())
        self.assertEqual(self.finding("non_akp_item_created")["created_by"], "Bookkeeper")

    def test_catalogue_sync_item_in_mapping_is_info_only(self):
        receipts = self.root / "catalogue_sync"
        receipts.mkdir()
        (receipts / "run.json").write_text(json.dumps({"created": [{"qbo_item_id": "15032"}]}))
        self.run_guard(self.data())
        self.assertEqual(self.checks(), [])
        created = self.report["info"]["akp_item_created"]
        self.assertEqual(created["count"], 2)
        by_id = {s["id"]: s for s in created["sample"]}
        self.assertTrue(by_id["15032"]["catalogue_sync_receipt"])
        self.assertFalse(by_id["15033"]["catalogue_sync_receipt"])

    def test_akp_inventory_on_120_asset_is_alert(self):
        items = [akp_item("15040", "AKP-1596500", "MILO 400G", asset="1150040011")]
        rows = [*MAPPING, mapping_row("15040", "AKP-1596500", "MILO 400G")]
        write_csv(self.mapping, rows, MAPPING_COLUMNS)
        self.run_guard(self.data(items=items))
        self.assertEqual(self.checks(), ["akp_wrong_asset_account"])
        self.assertIn("120100 - Grocery", self.finding("akp_wrong_asset_account")["detail"])

    def test_akp_sku_type_mismatch_and_unmapped_and_early_start(self):
        items = [akp_item("15041", "AKP-1596600", "FANTA 50CL", item_type="NonInventory"),
                 akp_item("15042", "AKP-1596601", "SPRITE 50CL", InvStartDate="2026-09-30")]
        self.run_guard(self.data(items=items))
        checks = self.checks()
        self.assertIn("akp_type_mismatch", checks)
        self.assertIn("akp_item_not_in_mapping", checks)
        self.assertIn("akp_inv_start_date", checks)

    def test_near_duplicate_name_alert_has_suggestion(self):
        items = [{"Id": "20002", "Name": "SNIPER FLYING INSECT KILLER 600ml", "Sku": "", "Type": "Inventory",
                  "Active": True, "MetaData": NEW, "QtyOnHand": 0}]
        self.run_guard(self.data(items=items))
        dup = self.finding("possible_duplicate")
        self.assertEqual(dup["severity"], guard_mod.ALERT)
        self.assertIn("possible duplicate of AKP-1596315", dup["suggestion"])

    def test_duplicate_name_by_case_and_whitespace(self):
        items = [akp_item("15043", "AKP-1596700", "coca  cola 50cl")]
        self.run_guard(self.data(items=items))
        self.assertIn("duplicate_name", self.checks())

    def test_duplicate_between_legacy_items_is_warn(self):
        twin = {**copy.deepcopy(BASE_ITEMS[4]), "Id": "901", "Name": LEGACY_PREFIX + "COCA COLA  50CL",
                "MetaData": UPDATED}
        self.run_guard(self.data(items=[twin]))
        self.assertEqual(self.checks(), [])
        self.assertIn("duplicate_name", self.checks(guard_mod.WARN))

    def test_unrenamed_legacy_items_are_one_aggregated_warn(self):
        items = [{"Id": str(700 + n), "Name": f"OLD THING {n}", "Type": "Inventory", "Active": True,
                  "QtyOnHand": n, "MetaData": OLD} for n in range(3)]
        self.run_guard(self.data(items=items))
        self.assertEqual(self.checks(guard_mod.WARN).count("legacy_not_renamed"), 1)
        self.assertEqual(self.report["legacy_not_renamed"]["count"], 3)
        self.assertEqual(self.report["legacy_not_renamed"]["qty_nonzero"], 2)
        self.assertEqual(self.checks(), [])

    def test_legacy_renamed_back_and_snapshot_changes(self):
        renamed_back = {**copy.deepcopy(BASE_ITEMS[4]), "Name": "COCA COLA 50CL OLD", "MetaData": UPDATED}
        data = self.data()
        data["Item"][4] = renamed_back
        self.run_guard(data)
        self.assertIn("legacy_not_frozen", self.checks())

        (self.state / "legacy_snapshot.json").write_text(json.dumps(
            {"900": {"name": LEGACY_PREFIX + "COCA COLA 50CL", "active": False, "qty": 10}}))
        data["Item"][4] = {**copy.deepcopy(BASE_ITEMS[4]), "MetaData": UPDATED}
        self.run_guard(data)
        self.assertEqual(sorted(self.checks()), ["legacy_qty_changed", "legacy_reactivated"])

    def test_service_item_created_is_info(self):
        items = [{"Id": "20003", "Name": "Delivery", "Type": "Service", "Active": True, "MetaData": NEW}]
        self.run_guard(self.data(items=items))
        self.assertEqual(self.report["info"]["Service_item_created"]["count"], 1)
        self.assertEqual(self.checks(), [])


class TransactionChecksTest(ItemGuardTestCase):
    def test_bill_line_on_legacy_item_is_alert(self):
        self.run_guard(self.data(Bill=[bill("301", [item_line("900")])]))
        alert = self.finding("legacy_item_in_october")
        self.assertEqual((alert["entity"], alert["doc_number"], alert["party"]), ("Bill", "PO-301", "GPFH"))
        self.assertEqual(alert["line_item"], LEGACY_PREFIX + "COCA COLA 50CL")
        self.assertEqual(alert["amount"], 500.0)

    def test_sales_receipt_on_catch_all_in_october_is_alert(self):
        receipt = {"Id": "80501", "DocNumber": "SR-20261002-0001", "TxnDate": "2026-10-02", "MetaData": NEW,
                   "Line": [item_line("15030", 1000.0, "SalesItemLineDetail"),
                            item_line("15032", 200.0, "SalesItemLineDetail")]}
        self.run_guard(self.data(SalesReceipt=[receipt]))
        self.assertEqual(self.checks(), ["catch_all_item_in_october"])

    def test_september_backfill_on_catch_all_is_not_alert(self):
        receipt = {"Id": "80400", "DocNumber": "SR-20260930-0001", "TxnDate": "2026-09-30", "MetaData": NEW,
                   "Line": [item_line("15030", 1000.0, "SalesItemLineDetail")]}
        self.run_guard(self.data(SalesReceipt=[receipt]))
        self.assertEqual(self.checks(), [])
        self.assertEqual(self.report["info"]["pre_october_SalesReceipt_changed"]["count"], 1)

    def test_bill_on_mapped_akp_items_is_clean_and_bills_sync_counted(self):
        lines = [item_line("15032"), item_line("15033")]
        self.run_guard(self.data(Bill=[bill("302", lines, note="PO 3901 created by bills_sync v1")]))
        self.assertEqual(self.checks(), [])
        self.assertEqual(self.report["info"]["bills_sync_bills"]["count"], 1)

    def test_expense_line_to_120_or_77_is_alert(self):
        purchase = {"Id": "401", "TxnDate": "2026-10-02", "EntityRef": {"name": "Market"}, "MetaData": NEW,
                    "Line": [account_line("1150040011"), account_line("77"), account_line("200")]}
        self.run_guard(self.data(Purchase=[purchase]))
        self.assertEqual(self.checks(), ["expense_line_to_inventory_account"] * 2)

    def test_unmapped_item_in_october_is_alert(self):
        items = [{"Id": "20004", "Name": "Discount", "Type": "Service", "Active": True, "MetaData": OLD}]
        invoice = {"Id": "501", "TxnDate": "2026-10-02", "CustomerRef": {"name": "Hotel"}, "MetaData": NEW,
                   "Line": [item_line("20004", 50.0, "SalesItemLineDetail")]}
        self.run_guard(self.data(items=items, Invoice=[invoice]))
        self.assertEqual(self.checks(), ["unmapped_item_in_october"])

    def test_journal_to_inventory_is_warn(self):
        journal = {"Id": "80493", "DocNumber": "INV-EQ-2026-10-01", "TxnDate": "2026-10-01", "MetaData": NEW,
                   "Line": [{"Amount": 100.0, "JournalEntryLineDetail": {"PostingType": "Credit",
                                                                         "AccountRef": {"value": "77"}}},
                            {"Amount": 100.0, "JournalEntryLineDetail": {"PostingType": "Debit",
                                                                         "AccountRef": {"value": "200"}}}]}
        self.run_guard(self.data(JournalEntry=[journal]))
        warn = self.finding("journal_to_inventory_account")
        self.assertEqual((warn["severity"], warn["amount"]), (guard_mod.WARN, -100.0))

    def test_backdated_bill_is_warn(self):
        self.run_guard(self.data(Bill=[bill("303", [item_line("900")], txn_date="2026-09-20")]))
        self.assertEqual(self.checks(guard_mod.WARN).count("backdated_txn"), 1)
        self.assertEqual(self.checks(), [])


class BalancesAndRunTest(ItemGuardTestCase):
    def test_120_nonzero_alert_and_negative_stock_warn(self):
        accounts = copy.deepcopy(BASE_ACCOUNTS)
        accounts[3]["CurrentBalance"] = 25.5
        code = self.run_guard(self.data(accounts=accounts), argv=["--fail-on-alert"])
        self.assertEqual(code, guard_mod.EXIT_ALERT)
        self.assertEqual(self.checks(), ["inventory_120_nonzero"])
        self.assertEqual(self.report["negative_stock"]["count"], 1)
        self.assertEqual(self.report["negative_stock"]["total_value"], -300.0)
        self.assertIn("akp_negative_stock", self.checks(guard_mod.WARN))

    def test_exit_zero_without_fail_flag(self):
        accounts = copy.deepcopy(BASE_ACCOUNTS)
        accounts[2]["CurrentBalance"] = -1
        self.assertEqual(self.run_guard(self.data(accounts=accounts)), 0)

    def test_cursor_advances_only_on_success(self):
        cursor = self.state / "cursor.json"
        cursor.write_text(json.dumps({"since": SINCE}))

        class Boom(Exception):
            pass

        client = fake_client(self.data())
        original = client.query_all

        def failing(sql, entity, page_size=1000):
            if entity == "Bill":
                raise Boom("QBO down")
            return original(sql, entity, page_size)

        client.query_all = failing
        with self.assertRaises(Boom):
            guard_mod.main(["--out", str(self.root / "fail")], client=client, state=self.state,
                           mapping_path=self.mapping, slack=self.sent.append)
        self.assertEqual(json.loads(cursor.read_text())["since"], SINCE)
        self.assertFalse((self.state / "legacy_snapshot.json").exists())
        self.assertEqual(self.sent, [])

        self.run_guard(self.data())
        saved = json.loads(cursor.read_text())
        self.assertEqual(saved["since"], "2026-10-02T13:55:00+01:00")
        self.assertEqual(saved["previous_since"], SINCE)
        self.assertIn("900", json.loads((self.state / "legacy_snapshot.json").read_text()))

    def test_stored_cursor_is_used_without_since(self):
        (self.state / "cursor.json").write_text(json.dumps({"since": "2026-10-05T00:00:00+01:00"}))
        guard_mod.main(["--out", str(self.root / "out"), "--no-slack"], client=fake_client(self.data()),
                       state=self.state, mapping_path=self.mapping, slack=self.sent.append)
        report = json.loads((self.root / "out" / "report.json").read_text())
        self.assertEqual(report["since"], "2026-10-05T00:00:00+01:00")
        self.assertEqual(report["items_created"], 0)

    def test_client_never_issues_non_get(self):
        data = self.data(Bill=[bill("301", [item_line("900")])])
        data["Item"] = [akp_item(str(30000 + n), f"AKP-{n}", f"BULK ITEM {n}", meta=OLD) for n in range(2500)]
        client = fake_client(data)
        self.run_guard(data, client=client)
        queries = client._requests.queries
        self.assertTrue(all(q.lower().startswith("select") for q in queries))
        item_pages = [q for q in queries if q.startswith("select * from Item startposition")]
        self.assertEqual(len(item_pages), 3)
        self.assertEqual(self.report["items_loaded"]["active"], 2500)
        with self.assertRaises(AssertionError):
            client._requests.post("https://example.invalid")

    def test_mapping_missing_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            guard_mod.main(["--out", str(self.root / "out")], client=fake_client(self.data()), state=self.state,
                           mapping_path=self.root / "missing.csv", slack=self.sent.append)
        self.assertFalse((self.state / "cursor.json").exists())


class SlackTest(ItemGuardTestCase):
    def test_slack_concise_with_counts_and_sent_on_alert(self):
        self.run_guard(self.data(Bill=[bill("301", [item_line("900"), item_line("15030")])]))
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0]
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("item_guard company_a: 2 ALERT, 1 WARN, 2 INFO since"))
        self.assertIn("ALERT: catch_all_item_in_october x1, legacy_item_in_october x1", text)
        self.assertIn("Negative AKP stock: 1 items", text)
        self.assertIn("- Bill PO-301 2026-10-02: legacy_item_in_october " + LEGACY_PREFIX + "COCA COLA 50CL", text)
        self.assertIn("INFO: akp_item_created x2", text)
        self.assertLessEqual(len(lines), 12)

    def test_slack_skipped_when_only_info(self):
        data = self.data()
        data["Item"][2]["QtyOnHand"] = 0
        self.run_guard(data)
        self.assertEqual(self.report["counts"]["ALERT"] + self.report["counts"]["WARN"], 0)
        self.assertEqual(self.sent, [])

    def test_no_slack_flag(self):
        self.run_guard(self.data(Bill=[bill("301", [item_line("900")])]), argv=["--no-slack"])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
