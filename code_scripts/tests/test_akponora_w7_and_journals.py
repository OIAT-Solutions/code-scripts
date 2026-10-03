"""W7 create tool + journal poster: payloads, preflight, resumability, fill-ids, journal gating.

All QBO HTTP is faked; nothing here can reach Intuit.
"""
import csv
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

from code_scripts.product_conversion import ProductConversionRegistry
from code_scripts.scripts.akponora_cutover import post_journal as pj
from code_scripts.scripts.akponora_cutover import w7_create_items as w7

MAP_COLS = ["Row ID", "EPOS Product ID", "EPOS Existing SKU", "EPOS Name", "Pipeline Status", "Review Status",
            "Target QBO Item Type", "Target QBO Name", "Target QBO SKU", "Target QBO Item Id",
            "Staff Approved Sale Multiplier", "Effective Date", "Approved By", "Canonical Family Key",
            "Canonical Unit", "Staff Approved Purchase Multiplier"]
INV_COLS = ["Name", "Sku", "Type", "TrackQtyOnHand", "QtyOnHand", "InvStartDate", "PurchaseCost",
            "PurchaseTaxIncluded", "UnitPrice", "SalesTaxIncluded", "Taxable", "SalesTaxCodeId", "PurchaseTaxCodeId",
            "AssetAccountId", "IncomeAccountId", "ExpenseAccountId (COGS)", "EPOS Category", "Canonical Unit",
            "Owner EPOS Product ID", "Owner multiplier", "Description", "ParentRef", "SubItem", "Approval"]
NON_COLS = ["Name", "Sku", "Type", "TrackQtyOnHand", "UnitPrice", "SalesTaxIncluded", "Taxable", "SalesTaxCodeId",
            "PurchaseCost", "PurchaseTaxIncluded", "PurchaseTaxCodeId", "IncomeAccountId",
            "ExpenseAccountId (purchase)", "EPOS Category", "EPOS Product ID", "EPOS Name", "Description",
            "ParentRef", "SubItem"]
STOCK_COLS = ["Name", "CategoryName", "MeasuredCurrentStock", "CurrentVolume", "TotalStock", "MeasuredCostPrice",
              "TotalCost"]
APPROVER = "Owner (chat 2026-09-26)"


def write_csv(path, cols, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def map_row(pid, name, typ, target, sku, mult="1", family="", unit="Each", pm="1"):
    return {"Row ID": f"EPOS-{pid}", "EPOS Product ID": pid, "EPOS Existing SKU": "", "EPOS Name": name,
            "Pipeline Status": "TIER_A", "Review Status": "Approved", "Target QBO Item Type": typ,
            "Target QBO Name": target, "Target QBO SKU": sku, "Target QBO Item Id": "",
            "Staff Approved Sale Multiplier": mult, "Effective Date": "2026-10-01", "Approved By": APPROVER,
            "Canonical Family Key": family, "Canonical Unit": unit, "Staff Approved Purchase Multiplier": pm}


def inv_row(name, sku, owner, mult, cost, price, income="1150040034", cogs="1150040034", tax="2"):
    income = {"1150040034": "1150040031"}.get(income, income)
    return {"Name": name, "Sku": sku, "Type": "Inventory", "TrackQtyOnHand": "TRUE", "QtyOnHand": "",
            "InvStartDate": "2026-10-01", "PurchaseCost": cost, "PurchaseTaxIncluded": "FALSE", "UnitPrice": price,
            "SalesTaxIncluded": "TRUE", "Taxable": "TRUE" if tax == "2" else "FALSE", "SalesTaxCodeId": tax,
            "PurchaseTaxCodeId": tax, "AssetAccountId": "77", "IncomeAccountId": income,
            "ExpenseAccountId (COGS)": cogs, "EPOS Category": "DRINKS & BEVERAGES", "Canonical Unit": "Each",
            "Owner EPOS Product ID": owner, "Owner multiplier": mult, "Description": f"EPOS master {owner}",
            "ParentRef": "", "SubItem": "FALSE", "Approval": "rule a"}


def fixture(tmp: Path, *, eggs_category="PROVISIONS AND CEREALS"):
    """Two Inventory families (crate-of-24 can; negative-stock water) + one NonInventory."""
    catalogue = [
        {"Id": 100, "Name": "COKE CAN*24", "IsStockTracked": True, "VolumeOfSale": 24, "CostPriceExTax": 4800.0,
         "SalePriceIncTax": 9600.0, "CostPriceTaxGroupName": "VAT", "CategoryName": "DRINKS & BEVERAGES"},
        {"Id": 101, "Name": "COKE CAN", "IsStockTracked": False, "VolumeOfSale": None, "CostPriceExTax": 200.0,
         "SalePriceIncTax": 400.0, "CostPriceTaxGroupName": "VAT", "CategoryName": "DRINKS & BEVERAGES"},
        {"Id": 200, "Name": "WATER 75CL", "IsStockTracked": True, "VolumeOfSale": None, "CostPriceExTax": 0,
         "SalePriceIncTax": 300.0, "CostPriceTaxGroupName": "VAT", "CategoryName": "DRINKS & BEVERAGES"},
        {"Id": 300, "Name": "EGGS LOOSE", "IsStockTracked": False, "VolumeOfSale": None, "CostPriceExTax": 150.0,
         "SalePriceIncTax": 200.0, "CostPriceTaxGroupName": "VAT", "CategoryName": eggs_category},
    ]
    (tmp / "catalogue.json").write_text(json.dumps(catalogue))
    write_csv(tmp / "stock.csv", STOCK_COLS, [
        {"Name": "COKE CAN*24", "MeasuredCurrentStock": "2", "CurrentVolume": "5", "TotalStock": "2.20833",
         "MeasuredCostPrice": "4800", "TotalCost": "10600"},
        {"Name": "WATER 75CL", "MeasuredCurrentStock": "-3", "CurrentVolume": "0", "TotalStock": "-3",
         "MeasuredCostPrice": "0", "TotalCost": "0"},
        {"Name": "Total:", "TotalCost": "10600"},
    ])
    write_csv(tmp / "mapping.csv", MAP_COLS, [
        map_row("100", "COKE CAN*24", "Inventory", "COKE CAN", "AKP-100", mult="24", family="AKP-100", pm="24"),
        map_row("101", "COKE CAN", "Inventory", "COKE CAN", "AKP-100", family="AKP-100", pm="24"),
        map_row("200", "WATER 75CL", "Inventory", "WATER 75CL", "AKP-200", family="AKP-200"),
        map_row("300", "EGGS LOOSE", "NonInventory", "EGGS LOOSE", "AKP-NS-300", unit=""),
    ])
    write_csv(tmp / "inv.csv", INV_COLS, [
        inv_row("COKE CAN", "AKP-100", "100", "24", "200.00000", "400.00"),
        inv_row("WATER 75CL", "AKP-200", "200", "1", "0.00000", "300.00"),
    ])
    write_csv(tmp / "non.csv", NON_COLS, [
        {"Name": "EGGS LOOSE", "Sku": "AKP-NS-300", "Type": "NonInventory", "TrackQtyOnHand": "FALSE",
         "UnitPrice": "200.00", "SalesTaxIncluded": "TRUE", "Taxable": "TRUE", "SalesTaxCodeId": "2",
         "PurchaseCost": "150.00000", "PurchaseTaxIncluded": "FALSE", "PurchaseTaxCodeId": "2",
         "IncomeAccountId": "1150040024", "ExpenseAccountId (purchase)": "74", "EPOS Category": eggs_category,
         "EPOS Product ID": "300", "EPOS Name": "EGGS LOOSE", "Description": "EPOS non-stock product 300",
         "ParentRef": "", "SubItem": "FALSE"},
    ])


def build(tmp: Path, as_of="2026-09-30"):
    return w7.build_plan(mapping_path=tmp / "mapping.csv", inventory_path=tmp / "inv.csv",
                         noninventory_path=tmp / "non.csv", stock_path=tmp / "stock.csv",
                         catalogue_path=tmp / "catalogue.json", as_of=as_of)


# ---------------------------------------------------------------- fake QBO
class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


ACCOUNTS = {
    "77": {"Id": "77", "Name": "Inventory Asset", "AccountType": "Other Current Asset", "AccountSubType": "Inventory", "Active": True, "CurrentBalance": 1000},
    "76": {"Id": "76", "Name": "200000 - Cost of sales", "AccountType": "Cost of Goods Sold", "Active": True},
    "86": {"Id": "86", "Name": "300150", "AccountType": "Equity", "Active": True},
    "74": {"Id": "74", "Name": "200100", "AccountType": "Cost of Goods Sold", "Active": True},
    "1150040034": {"Id": "1150040034", "Name": "200202", "AccountType": "Cost of Goods Sold", "Active": True},
    "1150040031": {"Id": "1150040031", "Name": "400202", "AccountType": "Income", "Active": True},
    "1150040024": {"Id": "1150040024", "Name": "400100", "AccountType": "Income", "Active": True},
    "999": {"Id": "999", "Name": "old", "AccountType": "Income", "Active": False},
}


class FakeQBO:
    """Minimal in-memory QBO: items, accounts, tax codes, journals, TrialBalance for 77."""

    def __init__(self, items=(), journals=(), ia=Decimal("1000"), fail_first_post=None):
        self.items = {it["Id"]: dict(it) for it in items}
        self.journals = {j["Id"]: dict(j) for j in journals}
        self.ia = ia
        self.next_id = 5000
        self.calls = []
        self.fail_first_post = fail_first_post
        self.mutate_created = None

    def request(self, method, url, params=None, headers=None, data=None, timeout=None):
        params = params or {}
        path = url.split("/v3/company/x", 1)[-1]
        self.calls.append((method, path, dict(params)))
        if method == "GET" and path == "/query":
            return self._query(unquote(params["query"]))
        if method == "GET" and path.startswith("/account/"):
            a = ACCOUNTS.get(path.rsplit("/", 1)[1])
            return Resp(200, {"Account": {**a, "CurrentBalance": float(self.ia)} if a and a["Id"] == "77" else a}) if a else Resp(400, {"Fault": {}})
        if method == "GET" and path == "/reports/TrialBalance":
            return Resp(200, {"Rows": {"Row": [{"ColData": [{"id": "77", "value": "Inventory Asset"},
                                                             {"value": str(self.ia)}, {"value": ""}]}]}})
        if method == "GET" and path.startswith("/item/"):
            return Resp(200, {"Item": self.items[path.rsplit("/", 1)[1]]})
        if method == "GET" and path.startswith("/journalentry/"):
            return Resp(200, {"JournalEntry": self.journals[path.rsplit("/", 1)[1]]})
        if method == "GET" and path == "/preferences":
            return Resp(200, {"Preferences": {"AccountingInfoPrefs": {"BookCloseDate": "2026-08-31"}}})
        if method == "POST" and path == "/item":
            if self.fail_first_post:
                code, self.fail_first_post = self.fail_first_post, None
                return Resp(code, {"Fault": {"Error": [{"Message": "boom"}]}})
            body = json.loads(data)
            self.next_id += 1
            item = {**body, "Id": str(self.next_id), "SyncToken": "0", "Active": True,
                    "FullyQualifiedName": body["Name"]}
            if self.mutate_created:
                item.update(self.mutate_created)
            self.items[item["Id"]] = item
            if body["Type"] == "Inventory":
                self.ia += Decimal(str(body["QtyOnHand"])) * Decimal(str(body["PurchaseCost"]))
            return Resp(200, {"Item": item})
        if method == "POST" and path == "/journalentry":
            body = json.loads(data)
            self.next_id += 1
            je = {**body, "Id": str(self.next_id), "SyncToken": "0"}
            self.journals[je["Id"]] = je
            return Resp(200, {"JournalEntry": je})
        raise AssertionError(f"unexpected {method} {path}")

    def _query(self, sql):
        if sql.startswith("select * from TaxCode"):
            return Resp(200, {"QueryResponse": {"TaxCode": [{"Id": "2", "Active": True}, {"Id": "7", "Active": True}]}})
        if "from JournalEntry" in sql:
            doc = re.search(r"DocNumber = '([^']*)'", sql).group(1)
            return Resp(200, {"QueryResponse": {"JournalEntry": [j for j in self.journals.values() if j["DocNumber"] == doc]}})
        if "from Item" in sql:
            m = re.search(r"where (Sku|Name) = '([^']*)'", sql)
            rows = list(self.items.values())
            if m:
                rows = [it for it in rows if str(it.get(m.group(1), "")) == m.group(2)]
            start = int(re.search(r"startposition (\d+)", sql).group(1)) if "startposition" in sql else 1
            return Resp(200, {"QueryResponse": {"Item": rows[start - 1:start - 1 + 1000]}})
        raise AssertionError(sql)

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def client(fake, writes=False):
    return w7.QBOClient(fake, lambda: "tok", "https://x/v3/company/x", allow_writes=writes, sleep=lambda s: None)


# ---------------------------------------------------------------- W7 tests
class W7PayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        fixture(self.tmp)

    def entries(self):
        plan, entries, _ = build(self.tmp)
        self.assertEqual(plan["blocking"], [])
        return plan, {e["sku"]: e for e in entries}

    def test_inventory_fields_full_plus_loose_and_cost_per_canonical_unit(self):
        plan, e = self.entries()
        p = e["AKP-100"]["payload"]
        self.assertEqual(p["Type"], "Inventory")
        self.assertIs(p["TrackQtyOnHand"], True)
        self.assertEqual(p["QtyOnHand"], 53.0)  # 2 crates x 24 + 5 loose
        self.assertEqual(p["PurchaseCost"], 200.0)  # 4800 / 24, ex-tax
        self.assertEqual(p["UnitPrice"], 400.0)
        self.assertEqual(p["InvStartDate"], "2026-10-01")
        self.assertEqual(p["AssetAccountRef"], {"value": "77"})
        self.assertEqual(p["ExpenseAccountRef"], {"value": "1150040034"})
        self.assertEqual(p["IncomeAccountRef"], {"value": "1150040031"})
        self.assertEqual((p["SalesTaxCodeRef"], p["PurchaseTaxCodeRef"]), ({"value": "2"}, {"value": "2"}))
        self.assertIs(p["PurchaseTaxIncluded"], False)
        self.assertNotIn("ParentRef", p)
        self.assertEqual(e["AKP-100"]["epos_product_ids"], ["100", "101"])
        self.assertEqual(plan["V_opening_value_ex_tax_item_rounded_2dp"], "10600.00")

    def test_negative_stock_floors_to_zero_and_zero_cost_is_flagged(self):
        _, e = self.entries()
        w = e["AKP-200"]
        self.assertEqual(w["payload"]["QtyOnHand"], 0.0)
        self.assertIn("NEGATIVE_ZERO_FLOOR", w["flags"])
        self.assertIn("ZERO_COST", w["flags"])
        self.assertEqual(w["value"], "0.00")

    def test_noninventory_fields(self):
        _, e = self.entries()
        p = e["AKP-NS-300"]["payload"]
        self.assertEqual(p["Type"], "NonInventory")
        self.assertEqual(p["Sku"], "AKP-NS-300")
        self.assertEqual(p["IncomeAccountRef"], {"value": "1150040024"})
        self.assertEqual(p["ExpenseAccountRef"], {"value": "74"})
        for absent in ("QtyOnHand", "InvStartDate", "AssetAccountRef", "TrackQtyOnHand", "ParentRef"):
            self.assertNotIn(absent, p)
        self.assertEqual((p["UnitPrice"], p["PurchaseCost"], p["PurchaseTaxIncluded"]), (200.0, 150.0, False))

    def test_rehearsal_label_and_unit_change_blocks(self):
        plan, _, _ = build(self.tmp, as_of="2026-09-25")
        self.assertTrue(plan["rehearsal"])
        cat = json.loads((self.tmp / "catalogue.json").read_text())
        cat[0]["VolumeOfSale"] = 12
        (self.tmp / "catalogue.json").write_text(json.dumps(cat))
        plan, _, _ = build(self.tmp)
        self.assertTrue(any("VolumeOfSale changed" in b for b in plan["blocking"]))

    def test_collision_refusal_in_preflight(self):
        _, entries, _ = build(self.tmp)
        legacy = [
            {"Id": "11", "Name": "COKE CAN", "FullyQualifiedName": "DRINKS & BEVERAGES:COKE CAN", "Type": "Inventory",
             "Active": True, "ParentRef": {"value": "13"}},
            {"Id": "12", "Name": "WATER 75CL", "FullyQualifiedName": "WATER 75CL", "Type": "Inventory", "Active": True},
            {"Id": "13", "Name": "OTHER", "FullyQualifiedName": "OTHER", "Sku": "AKP-NS-300", "Type": "Service", "Active": False},
        ]
        pf = w7.preflight(client(FakeQBO(legacy)), entries)
        self.assertEqual(pf["collision_counts"], {"LEAF_SOFT": 1, "FQN_HARD": 1, "SKU": 1})
        self.assertEqual(pf["account_problems"], [])
        self.assertEqual(pf["tax_problems"], [])

    def test_cli_dry_run_refuses_collisions_and_never_posts(self):
        fake = FakeQBO([{"Id": "12", "Name": "WATER 75CL", "FullyQualifiedName": "WATER 75CL", "Type": "Inventory", "Active": True}])
        out = self.tmp / "out"
        orig = w7.QBOClient.for_company_a
        w7.QBOClient.for_company_a = classmethod(lambda cls, allow_writes=False, max_rps=5: client(fake, allow_writes))
        try:
            args = ["create", "--mapping", str(self.tmp / "mapping.csv"), "--inventory-list", str(self.tmp / "inv.csv"),
                    "--noninventory-list", str(self.tmp / "non.csv"), "--stock-report", str(self.tmp / "stock.csv"),
                    "--catalogue", str(self.tmp / "catalogue.json"), "--as-of", "2026-09-30", "--out", str(out)]
            with redirect_stdout(io.StringIO()):
                rc = w7.main(args)
            self.assertEqual(rc, 1)
            summary = json.loads((out / "summary.json").read_text())
            self.assertTrue(any("collisions" in r for r in summary["refused"]))
            self.assertEqual(fake.posts(), [])
            sha = summary["payloads_sha256"]
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
                w7.main(args + ["--execute", "--approval-ref", "chat yes", "--expect-payloads-sha", sha])
            self.assertIn("rehearsal", str(cm.exception))  # stock file name not dated 2026_09_30
            self.assertEqual(fake.posts(), [])
        finally:
            w7.QBOClient.for_company_a = orig

    def test_dry_run_client_refuses_writes(self):
        with self.assertRaises(RuntimeError):
            client(FakeQBO()).post_json("/item", {}, "rid")


class W7ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        fixture(self.tmp)
        _, self.entries, _ = build(self.tmp)
        self.out = self.tmp / "out"
        self.out.mkdir()

    def test_pilot_then_resume_skips_done_and_adopts_existing_sku(self):
        fake = FakeQBO()
        s1 = w7.execute(client(fake, True), self.entries, self.out, approval_ref="chat", limit=1, as_of_tb="2026-10-01")
        self.assertEqual((s1["processed_this_run"], s1["stopped"], s1["B"]), (1, None, "1000"))
        self.assertEqual(Decimal(s1["C_actual"]), Decimal("10600"))
        self.assertEqual(Decimal(s1["C_difference"]), 0)
        self.assertEqual(len(fake.posts()), 1)
        # Simulate a crash after POST of item 2: it exists in QBO but not in results.csv.
        body = dict(self.entries[1]["payload"], Id="7777", SyncToken="0", Active=True, FullyQualifiedName="WATER 75CL")
        fake.items["7777"] = body
        s2 = w7.execute(client(fake, True), self.entries, self.out, approval_ref="chat", limit=None, as_of_tb="2026-10-01")
        self.assertIsNone(s2["stopped"])
        self.assertEqual(len(fake.posts()), 2)  # item 1 skipped, item 2 adopted, only NonInventory posted
        results = w7.load_results(self.out / "results.csv")
        self.assertEqual([r["status"] for r in results], ["CREATED", "ADOPTED", "CREATED"])
        self.assertEqual(s2["B"], "1000")  # B pinned at the first run
        self.assertEqual(s2["pending_after_run"], 0)
        reg = w7.read_csv(self.out / "register.csv")
        self.assertEqual([r["QBO Item Id"] for r in reg], [results[0]["QBO Id"], "7777", results[2]["QBO Id"]])
        self.assertEqual(reg[0]["EPOS Product IDs"], "100 101")
        # a requestid accompanies every write
        self.assertTrue(all(c[2].get("requestid") for c in fake.posts()))

    def test_existing_mismatched_item_stops(self):
        fake = FakeQBO([{"Id": "9", "Name": "COKE CAN", "Sku": "AKP-100", "Type": "Inventory", "Active": True,
                         "TrackQtyOnHand": True, "InvStartDate": "2026-10-01", "QtyOnHand": 1,
                         "AssetAccountRef": {"value": "77"}}])
        s = w7.execute(client(fake, True), self.entries, self.out, approval_ref="chat", limit=None, as_of_tb="2026-10-01")
        self.assertIn("do not match", s["stopped"])
        self.assertEqual(fake.posts(), [])

    def test_verification_failure_stops_run(self):
        fake = FakeQBO()
        fake.mutate_created = {"AssetAccountRef": {"value": "1150040030"}}
        s = w7.execute(client(fake, True), self.entries, self.out, approval_ref="chat", limit=None, as_of_tb="2026-10-01")
        self.assertIn("failed verification", s["stopped"])
        self.assertEqual(len(fake.posts()), 1)

    def test_retry_after_5xx_uses_same_requestid(self):
        fake = FakeQBO(fail_first_post=503)
        w7.execute(client(fake, True), self.entries, self.out, approval_ref="chat", limit=1, as_of_tb="2026-10-01")
        posts = fake.posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2]["requestid"], posts[1][2]["requestid"])


class FillIdsTests(unittest.TestCase):
    def test_round_trip_loads_through_registry(self):
        tmp = Path(tempfile.mkdtemp())
        fixture(tmp)
        _, entries, _ = build(tmp)
        out = tmp / "out"
        out.mkdir()
        w7.execute(client(FakeQBO(), True), entries, out, approval_ref="chat", limit=None, as_of_tb="2026-10-01")
        res = w7.fill_ids(tmp / "mapping.csv", out / "register.csv", out / "approved_mapping_with_ids.csv")
        self.assertEqual((res["rules"], res["distinct_target_ids"]), (4, 3))
        reg = ProductConversionRegistry.from_csv(out / "approved_mapping_with_ids.csv")
        self.assertEqual(reg.source_sha256, res["sha256"])
        ids = {r["Target QBO SKU"]: r["QBO Item Id"] for r in w7.read_csv(out / "register.csv")}
        for rule in reg.rules:
            self.assertEqual(rule.target_qbo_item_id, ids[rule.target_qbo_sku])
        # header and row order preserved
        with open(out / "approved_mapping_with_ids.csv", encoding="utf-8-sig") as fh:
            self.assertEqual(next(csv.reader(fh)), MAP_COLS)

    def test_missing_register_id_refuses(self):
        tmp = Path(tempfile.mkdtemp())
        fixture(tmp)
        write_csv(tmp / "register.csv", w7.REGISTER_COLS, [
            {"Target QBO SKU": "AKP-100", "Target QBO Name": "COKE CAN", "Target QBO Item Type": "Inventory",
             "QBO Item Id": "1"}])
        with self.assertRaises(w7.StopRun):
            w7.fill_ids(tmp / "mapping.csv", tmp / "register.csv", tmp / "x.csv")


# ---------------------------------------------------------------- journal tests
def spec(amount_dr="100.00", amount_cr="100.00", doc="COGS-2026-09", status="REVIEWED"):
    return {"doc_number": doc, "txn_date": "2026-09-30", "private_note": "test", "status": status,
            "lines": [{"account_id": "77", "posting_type": "Debit", "amount": amount_dr, "description": "a"},
                      {"account_id": "76", "posting_type": "Credit", "amount": amount_cr, "description": "b"}]}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, s):
        p = self.tmp / "spec.json"
        p.write_text(json.dumps(s))
        return p

    def run_post(self, path, fake, **kw):
        with redirect_stdout(io.StringIO()) as buf:
            rc = pj.run_post(path, client=client(fake, kw.pop("writes", False)), receipt_dir=self.tmp, **kw)
        return rc, buf.getvalue()

    def test_balance_validation(self):
        self.assertEqual(pj.validate_spec(spec())["problems"], [])
        self.assertTrue(any("!=" in p for p in pj.validate_spec(spec(amount_cr="99.99"))["problems"]))
        with self.assertRaises(ValueError):
            pj.validate_spec(spec(amount_dr="100.001", amount_cr="100.001"))
        self.assertTrue(pj.validate_spec(spec(doc="CREATE-NIGHT-IA-OFFSET"))["problems"])  # 22 > 21 chars
        bad = spec()
        bad["lines"][1]["account_id"] = ""
        self.assertIn("line 2: account_id is blank", pj.validate_spec(bad)["problems"])
        payload = pj.validate_spec(spec())["payload"]
        self.assertEqual(payload["Line"][0]["JournalEntryLineDetail"]["TaxCodeRef"], {"value": "7"})

    def test_sha_gating_then_post_and_receipt(self):
        path, fake = self.write(spec()), FakeQBO()
        rc, out = self.run_post(path, fake, execute=False, expect_sha="")
        self.assertEqual(rc, 0)
        sha = re.search(r"payload_sha256 ([0-9a-f]{64})", out).group(1)
        with self.assertRaises(SystemExit):
            self.run_post(path, fake, execute=True, expect_sha="0" * 64, approval_ref="chat", writes=True)
        self.assertEqual(fake.posts(), [])
        rc, out = self.run_post(path, fake, execute=True, expect_sha=sha, approval_ref="chat", writes=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(fake.posts()), 1)
        self.assertEqual(fake.posts()[0][2]["requestid"], sha[:36])
        receipt = json.loads(next(self.tmp.glob("receipt_COGS-2026-09_*.json")).read_text())
        self.assertEqual((receipt["status"], receipt["payload_sha256"]), ("POSTED_VERIFIED", sha))

    def test_duplicate_docnumber_refused(self):
        fake = FakeQBO(journals=[{"Id": "75098", "DocNumber": "COGS-2026-09", "TxnDate": "2026-09-30", "Line": []}])
        path = self.write(spec())
        rc, out = self.run_post(path, fake, execute=False, expect_sha="")
        self.assertEqual(rc, 1)
        self.assertIn("already used", out)
        sha = pj.payload_sha(pj.validate_spec(spec())["payload"])
        with self.assertRaises(SystemExit):
            self.run_post(path, fake, execute=True, expect_sha=sha, approval_ref="chat", writes=True)
        self.assertEqual(fake.posts(), [])

    def test_draft_closed_period_and_inactive_account_refused(self):
        s = spec(status="DRAFT - placeholder amounts")
        s["txn_date"] = "2026-08-31"
        s["lines"][1]["account_id"] = "999"
        rc, out = self.run_post(self.write(s), FakeQBO(), execute=False, expect_sha="")
        self.assertEqual(rc, 1)
        for text in ("DRAFT", "close date", "inactive"):
            self.assertIn(text, out)

    def test_templates_are_drafts_that_never_validate_for_posting(self):
        folder = Path(pj.__file__).parent / "journal_templates"
        names = sorted(p.name for p in folder.glob("*.json"))
        self.assertEqual(names, ["cogs_2026_09.json", "create_night_ia_offset.json", "grni_2026_09.json",
                                 "sep_120xxx_clear.json"])
        for p in folder.glob("*.json"):
            checked = pj.validate_spec(json.loads(p.read_text()))
            self.assertTrue(checked["draft"], p.name)
            self.assertTrue(checked["problems"], p.name)

    def test_offset_spec_uses_create_night_offset(self):
        summary = {"B": "142028049.94", "B_plus_C": "302840649.34", "C_expected_registered_items": "160812599.40",
                   "pending_after_run": 0, "stopped": None}
        s = pj.offset_spec(summary, "160812599.40")
        self.assertEqual(pj.validate_spec(s)["problems"], [])
        by = {ln["account_id"]: ln for ln in s["lines"]}
        self.assertEqual((by["77"]["posting_type"], by["77"]["amount"]), ("Credit", "142028049.94"))
        self.assertEqual(by["86"]["posting_type"], "Debit")
        # V above B + C reverses the sides
        s = pj.offset_spec({**summary, "B": "0", "B_plus_C": "100", "C_expected_registered_items": "100"}, "150")
        by = {ln["account_id"]: ln for ln in s["lines"]}
        self.assertEqual((by["77"]["posting_type"], by["77"]["amount"]), ("Debit", "50.00"))
        with self.assertRaises(ValueError):  # unexplained C difference
            pj.offset_spec({**summary, "C_expected_registered_items": "1"}, "160812599.40")
        with self.assertRaises(ValueError):
            pj.offset_spec({**summary, "pending_after_run": 3}, "160812599.40")


if __name__ == "__main__":
    unittest.main()
