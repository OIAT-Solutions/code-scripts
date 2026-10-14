"""October NonInventory (non-stock, AKP-NS-) mapping rule for Company A.

EPOS products with no stock master (loose/weighed goods) map from 1 Oct to a
dedicated NonInventory item with SKU AKP-NS-{EPOS ProductID}. Every QBO call is faked.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

from code_scripts import qbo_upload
from code_scripts.conversion_contract import validate_live_item, validate_upload_frame
from code_scripts.cutover_drafts import noninventory_drafts
from code_scripts.product_conversion import ProductResolutionError, october_target_error
from code_scripts.scripts.install_conversion_mapping import install
from code_scripts.tests import test_cutover_review_fixes as harness
from code_scripts.tests.test_product_conversion import Config, approved_row, sales_frame, write_mapping
from code_scripts.transform import transform_dataframe_unified

NS_ITEM = {"Id": "90001", "Name": "FROZEN CHICKEN KG", "Type": "NonInventory", "Sku": "AKP-NS-4242",
           "Active": True}


def ns_row(**overrides):
    row = approved_row(**{
        "Target QBO Item Type": "Non-inventory", "Target QBO Item Id": "90001",
        "Target QBO Name": "FROZEN CHICKEN KG", "Target QBO SKU": "AKP-NS-4242",
        "Effective Date": "2026-10-01", "Staff Approved Sale Multiplier": "1",
        "EPOS Name": "FROZEN CHICKEN (KG)", "EPOS Product ID": "4242", "EPOS Existing SKU": "",
        "Canonical Family Key": "", "Canonical Unit": "", "Staff Approved Purchase Multiplier": "",
    })
    row.update(overrides)
    return row


class OctoberTargetRuleTests(unittest.TestCase):
    def test_rule_matrix(self):
        self.assertEqual(october_target_error("Inventory", "AKP-WINE", "90001", "Wine"), "")
        self.assertEqual(october_target_error("NonInventory", "AKP-NS-4242", "90002", "Eggs"), "")
        self.assertIn("AKP-NS-", october_target_error("NonInventory", "AKP-4242", "90002", "Eggs"))
        self.assertIn("not AKP-NS-", october_target_error("Inventory", "AKP-NS-4242", "90002", "Eggs"))
        self.assertTrue(october_target_error("NonInventory", "AKP-NS-", "90002", "Eggs"))
        self.assertTrue(october_target_error("Service", "AKP-NS-4242", "90002", "Eggs"))
        self.assertTrue(october_target_error("NonInventory", "LEGACY-1", "15031", "REDBULL"))
        self.assertIn("catch-all", october_target_error("NonInventory", "AKP-NS-1", "15030", "x"))
        self.assertIn("catch-all", october_target_error("NonInventory", "AKP-NS-1", "90009",
                                                        "AKP-UNMAPPED-EPOS-SALES"))


class NonInventoryContractTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.row = ns_row()
        self.path = write_mapping(self.root, [self.row])
        self.config = Config(self.path)
        self.config.product_conversion_fail_closed_from = date(2026, 10, 1)
        self.frame = sales_frame(Product="FROZEN CHICKEN (KG)", Quantity=2.5, ProductId="4242",
                                 **{"Date/Time": "2026-10-01 10:00:00"})

    def converted(self):
        return transform_dataframe_unified(self.frame, self.config)

    def test_october_noninventory_ns_mapping_validates_with_multiplier(self):
        out = self.converted()
        targets = validate_upload_frame(out, self.config)
        self.assertEqual(targets["FROZEN CHICKEN KG"],
                         {"Id": "90001", "Name": "FROZEN CHICKEN KG", "Sku": "AKP-NS-4242", "Type": "NonInventory"})
        self.assertEqual(float(out.iloc[0]["ItemQuantity"]), 2.5)
        self.row["Staff Approved Sale Multiplier"] = "2"
        write_mapping(self.root, [self.row])
        out = self.converted()
        self.assertEqual(float(out.iloc[0]["ItemQuantity"]), 5.0)
        validate_upload_frame(out, self.config)

    def test_october_noninventory_without_ns_sku_fails(self):
        self.row["Target QBO SKU"] = "AKP-4242"
        write_mapping(self.root, [self.row])
        with self.assertRaisesRegex(ProductResolutionError, "AKP-NS-"):
            self.converted()

    def test_october_inventory_with_ns_sku_fails(self):
        self.row.update({"Target QBO Item Type": "Inventory", "Canonical Family Key": "CHICKEN",
                         "Canonical Unit": "kg", "Staff Approved Purchase Multiplier": "1"})
        write_mapping(self.root, [self.row])
        with self.assertRaisesRegex(ProductResolutionError, "INVALID_OCTOBER_TARGET"):
            self.converted()

    def test_contract_rejects_tampered_registry_even_if_transform_skipped(self):
        # Transform against a valid map, then swap in a map where the NS rule is an Inventory target.
        out = self.converted()
        self.row.update({"Target QBO Item Type": "Inventory", "Canonical Family Key": "CHICKEN",
                         "Canonical Unit": "kg", "Staff Approved Purchase Multiplier": "1"})
        write_mapping(self.root, [self.row])
        with self.assertRaises(ValueError):
            validate_upload_frame(out, self.config)

    def test_october_catch_all_still_fails(self):
        self.frame.loc[0, "Product"] = "LOOSE RICE (unmapped)"
        self.frame.loc[0, "ProductId"] = "9999"
        with self.assertRaises(ProductResolutionError):
            self.converted()
        # An approved rule pointing at the catch-all Id is also refused.
        self.row.update({"Target QBO Item Id": "15030", "Target QBO Name": "AKP-UNMAPPED-EPOS-SALES"})
        write_mapping(self.root, [self.row])
        self.frame.loc[0, "Product"] = "FROZEN CHICKEN (KG)"
        self.frame.loc[0, "ProductId"] = "4242"
        with self.assertRaisesRegex(ProductResolutionError, "catch-all"):
            self.converted()

    def test_september_history_unchanged(self):
        self.frame.loc[0, "Date/Time"] = "2026-09-30 10:00:00"
        targets = validate_upload_frame(self.converted(), self.config)
        self.assertEqual(next(iter(targets.values()))["Id"], "15030")

    def test_live_item_validation_for_noninventory(self):
        expected = validate_upload_frame(self.converted(), self.config)["FROZEN CHICKEN KG"]
        validate_live_item(dict(NS_ITEM), expected)  # no qty/asset/InvStartDate required
        for field, value in [("Id", "90002"), ("Name", "Wrong"), ("Type", "Inventory"), ("Type", "Service"),
                             ("Sku", "AKP-4242"), ("Sku", ""), ("Active", False)]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    validate_live_item({**NS_ITEM, field: value}, expected)
        live = dict(NS_ITEM)
        live.pop("Active")
        with self.assertRaises(ValueError):
            validate_live_item(live, expected)

    def test_upload_resolution_reads_exact_noninventory_id(self):
        targets = validate_upload_frame(self.converted(), self.config)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"Item": dict(NS_ITEM)}
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=response) as request, \
             mock.patch.object(qbo_upload, "get_or_create_item_id") as create:
            result = {}
            qbo_upload.resolve_conversion_items(["FROZEN CHICKEN KG"], self.config, mock.Mock(),
                                                harness.REALM, result, exact_targets=targets)
        self.assertIn("/item/90001?", request.call_args.args[1])
        create.assert_not_called()
        self.assertEqual(result["FROZEN CHICKEN KG"]["type_label"], "existing_non_inventory")
        response.json.return_value = {"Item": {**NS_ITEM, "Sku": "LEGACY"}}
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=response):
            with self.assertRaises(ValueError):
                qbo_upload.resolve_conversion_items(["FROZEN CHICKEN KG"], self.config, mock.Mock(),
                                                    harness.REALM, {}, exact_targets=targets)

    def test_installer_accepts_noninventory_and_rejects_bad_ns(self):
        destination = self.root / "state/approved.csv"
        inventory = approved_row(**{"Row ID": "2", "Target QBO Item Type": "Inventory", "Target QBO Item Id": "90005",
                                    "Target QBO Name": "Wine bottle", "Target QBO SKU": "AKP-WINE",
                                    "Effective Date": "2026-10-01", "Staff Approved Sale Multiplier": "1",
                                    "EPOS Name": "Wine crate", "EPOS Product ID": "5555", "EPOS Existing SKU": "W"})
        write_mapping(self.root, [self.row, inventory])
        raw = self.path.read_bytes()
        receipt = install(self.path, destination, hashlib.sha256(raw).hexdigest(), "chat approval")
        self.assertEqual(receipt["rows"], 2)
        for bad in ({"Target QBO SKU": "AKP-4242"}, {"Target QBO Item Type": "Service"},
                    {"Target QBO Item Id": "15030"}):
            with self.subTest(bad=bad):
                write_mapping(self.root, [{**self.row, **bad}])
                raw = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    install(self.path, destination, hashlib.sha256(raw).hexdigest(), "chat approval")


class NonInventoryDraftTests(unittest.TestCase):
    def product(self, **overrides):
        base = {"epos_product_id": "4242.0", "name": "FROZEN CHICKEN  KG", "category": "Frozen Foods",
                "approved_by": "Accountant", "approval_ref": "26 Sep decision", "stock_tracked": "False"}
        base.update(overrides)
        return base

    def test_draft_payload_shape(self):
        result = noninventory_drafts([self.product(), self.product(epos_product_id="77", name="EGGS",
                                                                   category="PROVISIONS AND CEREALS",
                                                                   tax_code_id="2")])
        self.assertFalse(result["production_approved"])
        first = result["drafts"][0]["payload"]
        self.assertEqual(first, {"Name": "FROZEN CHICKEN KG", "Sku": "AKP-NS-4242", "Type": "NonInventory",
                                 "IncomeAccountRef": {"value": "1150040024"},
                                 "ExpenseAccountRef": {"value": "74"}})
        for forbidden in ("AssetAccountRef", "QtyOnHand", "InvStartDate", "TrackQtyOnHand"):
            self.assertNotIn(forbidden, first)
        self.assertEqual(result["drafts"][1]["payload"]["SalesTaxCodeRef"], {"value": "2"})
        self.assertEqual(october_target_error("NonInventory", first["Sku"], "1", first["Name"]), "")

    def test_draft_fails_closed(self):
        for bad in ({"category": "UNKNOWN"}, {"epos_product_id": ""}, {"epos_product_id": "ABC"},
                    {"approval_ref": ""}, {"stock_tracked": "True"}, {"name": "X" * 101}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    noninventory_drafts([self.product(**bad)])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            noninventory_drafts([self.product(), self.product(name="OTHER")])


class OctoberNonInventoryUploadTests(harness.CompanyAUploadHarness):
    """End to end: transform -> dry-run evidence -> manifest -> exact POST to the NS item."""

    def setUp(self):
        super().setUp()
        write_mapping(self.root, [ns_row()])
        self.mapping = self.root / "mapping.csv"
        os.environ["COMPANY_A_PRODUCT_CONVERSION_FILE"] = str(self.mapping)
        patcher = mock.patch.dict(harness.OCT_ITEM, NS_ITEM, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_october_noninventory_posts_to_exact_ns_item(self):
        raw = harness._raw("FROZEN CHICKEN (KG)", 3, 9000, "2026-10-01 12:00:00", "Cash", product_id="4242")
        csv_path = self.transform_to_csv(raw, "2026-10-01")
        code, posts, _ = self.run_upload(csv_path, "2026-10-01", dry_run=True)
        self.assertEqual((code, posts), (0, []))
        evidence = self.state / "conversion_preflight" / "company_a_sales_batch_2026-10-01.json"
        harness.OctoberControlTests.approve(self, evidence)
        code, posts, _ = self.run_upload(csv_path, "2026-10-01")
        self.assertEqual(code, 0)
        self.assertEqual(len(posts), 1)
        lines = [l for l in posts[0]["payload"]["Line"] if l.get("DetailType") == "SalesItemLineDetail"]
        self.assertEqual({l["SalesItemLineDetail"]["ItemRef"]["value"] for l in lines}, {"90001"})
        self.assertEqual(lines[0]["SalesItemLineDetail"]["Qty"], 3.0)

    def test_october_live_item_wrong_type_blocks_day(self):
        harness.OCT_ITEM["Type"] = "Inventory"
        raw = harness._raw("FROZEN CHICKEN (KG)", 3, 9000, "2026-10-01 12:00:00", "Cash", product_id="4242")
        csv_path = self.transform_to_csv(raw, "2026-10-01")
        with self.assertRaisesRegex(ValueError, "Type mismatch"):
            self.run_upload(csv_path, "2026-10-01", dry_run=True)
        self.assertFalse((self.state / "conversion_preflight" / "company_a_sales_batch_2026-10-01.json").exists())


if __name__ == "__main__":
    unittest.main()
