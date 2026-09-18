from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

from code_scripts.product_conversion import (
    MappingValidationError,
    ProductConversionRegistry,
    ProductResolutionError,
    apply_product_conversion_to_sales,
    audit_sales,
    build_qbo_catalog_plan,
)
from code_scripts.transform import transform_dataframe_unified
from code_scripts import qbo_upload


FIELDS = [
    "Row ID",
    "EPOS Product ID",
    "EPOS Existing SKU",
    "EPOS Name",
    "Pipeline Status",
    "Review Status",
    "Target QBO Item Type",
    "Target QBO Name",
    "Target QBO SKU",
    "Target QBO Item Id",
    "Staff Approved Sale Multiplier",
    "Effective Date",
    "Approved By",
]


def approved_row(**overrides):
    row = {
        "Row ID": "1",
        "EPOS Product ID": "1651777",
        "EPOS Existing SKU": "EPOS-BW-OUTER",
        "EPOS Name": "BACKWOODS DARK STOUT CIGARS (5X1)*40",
        "Pipeline Status": "PROVISIONAL",
        "Review Status": "Approved",
        "Target QBO Item Type": "Non-inventory",
        "Target QBO Name": "BACKWOODS DARK STOUT CIGAR EACH",
        "Target QBO SKU": "AKP-BW-DARK-EACH",
        "Target QBO Item Id": "",
        "Staff Approved Sale Multiplier": "200",
        "Effective Date": "2026-09-01",
        "Approved By": "Reviewer",
    }
    row.update(overrides)
    return row


def write_mapping(folder: Path, rows) -> Path:
    path = folder / "mapping.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def sales_frame(**overrides) -> pd.DataFrame:
    row = {
        "Customer Full Name": "",
        "Location Name": "Shop",
        "Quantity": 1,
        "Product": "BACKWOODS DARK STOUT CIGARS (5X1)*40",
        "Category": "PROVISIONS AND CEREALS",
        "Date/Time": "2026-09-01 10:00:00",
        "TOTAL Sales": 120000,
        "NET Sales": 111627.91,
        "Cost Price": 85000,
        "Tax": 8372.09,
        "Tender": "Cash",
        "Barcode": "071610302723",
    }
    row.update(overrides)
    return pd.DataFrame([row])


class Config:
    date_format = "%Y-%m-%d"
    trading_day_enabled = False
    deposit_account = "Undeposited Funds"
    tax_mode = "vat_inclusive_7_5"
    aggregate_products = True
    receipt_number_format = "date_tender_sequence"
    receipt_prefix = "SR"
    product_conversion_enabled = True
    product_conversion_allow_name_fallback = True
    product_conversion_catch_all_name = "AKP-UNMAPPED-EPOS-SALES"
    product_conversion_fail_closed_from = None

    def __init__(self, mapping: Path):
        self.product_conversion_file = mapping


class UploadConfig:
    product_conversion_enabled = True
    product_conversion_approved_item_ids = set()

    @staticmethod
    def get_qbo_config():
        return {"default_item_id": "1", "default_income_account_id": "1"}


class FakeResponse:
    status_code = 200

    def __init__(self, items):
        self._items = items

    def json(self):
        return {"QueryResponse": {"Item": self._items}}


class ProductConversionTests(unittest.TestCase):
    def test_explicit_multiplier_replaces_legacy_trailing_multiplier(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping = write_mapping(Path(tmp), [approved_row()])
            result = transform_dataframe_unified(sales_frame(), Config(mapping))

        self.assertEqual(result.loc[0, "Item(Product/Service)"], "BACKWOODS DARK STOUT CIGAR EACH")
        self.assertEqual(result.loc[0, "ItemQuantity"], 200)
        self.assertNotEqual(result.loc[0, "ItemQuantity"], 8000)
        self.assertEqual(result.loc[0, "*ItemAmount"], 120000)
        self.assertEqual(result.loc[0, "TOTAL Sales"], 120000)
        self.assertEqual(result.loc[0, "Cost Price"], 85000)
        self.assertEqual(result.loc[0, "ItemTaxAmount"], 8372.09)

    def test_product_id_has_priority_and_unmapped_id_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProductConversionRegistry.from_csv(
                write_mapping(Path(tmp), [approved_row()]),
                allow_name_fallback=True,
            )
            frame = sales_frame(ProductID="999999")
            with self.assertRaises(ProductResolutionError) as context:
                apply_product_conversion_to_sales(frame, registry)

        self.assertIn("UNMAPPED_PRODUCT_ID", str(context.exception))

    def test_provisional_mapping_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = approved_row(**{"Review Status": "Provisional"})
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [row]))
            audit = audit_sales(sales_frame(), registry)

        self.assertEqual(audit[0]["Status"], "BLOCK")
        self.assertIn("NOT_APPROVED", audit[0]["Reason"])

    def test_shared_barcode_is_not_a_lookup_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [approved_row()]))
            audit = audit_sales(sales_frame(Product="UNKNOWN PRODUCT"), registry)

        self.assertEqual(audit[0]["Status"], "BLOCK")
        self.assertIn("UNMAPPED_PRODUCT", audit[0]["Reason"])

    def test_unmapped_products_use_catch_all_and_keep_original_descriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping = write_mapping(Path(tmp), [approved_row()])
            frame = pd.concat(
                [
                    sales_frame(Product="UNKNOWN ONE", Quantity=2),
                    sales_frame(Product="UNKNOWN TWO", Quantity=3),
                ],
                ignore_index=True,
            )
            result = transform_dataframe_unified(frame, Config(mapping))

        self.assertEqual(len(result), 2)
        self.assertEqual(set(result["Item(Product/Service)"]), {"AKP-UNMAPPED-EPOS-SALES"})
        self.assertEqual(set(result["ItemQuantity"]), {2.0, 3.0})
        self.assertEqual(
            set(result["ItemDescription"]),
            {"Unmapped EPOS product: UNKNOWN ONE", "Unmapped EPOS product: UNKNOWN TWO"},
        )

    def test_epos_productid_column_resolves_approved_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            mapping = write_mapping(Path(tmp), [approved_row()])
            frame = sales_frame(
                Product="TILL BUTTON NAME",
                ProductId="1651777",
                Quantity=1,
            )
            result = transform_dataframe_unified(frame, Config(mapping))

        self.assertEqual(list(result["Item(Product/Service)"]), ["BACKWOODS DARK STOUT CIGAR EACH"])
        self.assertEqual(list(result["ItemQuantity"]), [200.0])

    def test_invalid_quantity_blocks_even_when_catch_all_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [approved_row()]))
            with self.assertRaises(ProductResolutionError) as context:
                apply_product_conversion_to_sales(
                    sales_frame(Product="UNKNOWN", Quantity="not-a-number"),
                    registry,
                    catch_all_name="AKP-UNMAPPED-EPOS-SALES",
                )

        self.assertIn("INVALID_QUANTITY", str(context.exception))

    def test_duplicate_approved_name_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                approved_row(**{"EPOS Product ID": "1", "EPOS Existing SKU": "ONE"}),
                approved_row(**{"Row ID": "2", "EPOS Product ID": "2", "EPOS Existing SKU": "TWO"}),
            ]
            path = write_mapping(Path(tmp), rows)
            with self.assertRaises(MappingValidationError):
                ProductConversionRegistry.from_csv(path)

    def test_uploader_accepts_existing_non_inventory_target(self):
        response = FakeResponse([{"Id": "77", "Name": "CANONICAL", "Type": "NonInventory"}])
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=response):
            result = qbo_upload.get_or_create_item_id(
                "CANONICAL", object(), "realm", UploadConfig(), {}
            )
        self.assertEqual(result, ("77", False, "existing_non_inventory", None))

    def test_uploader_rejects_legacy_inventory_name_collision(self):
        response = FakeResponse([{"Id": "88", "Name": "CANONICAL", "Type": "Inventory"}])
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=response):
            with self.assertRaises(RuntimeError) as context:
                qbo_upload.get_or_create_item_id(
                    "CANONICAL", object(), "realm", UploadConfig(), {}
                )
        self.assertIn("legacy QBO Type='Inventory'", str(context.exception))

    def test_uploader_does_not_implicitly_create_missing_mapped_item(self):
        response = FakeResponse([])
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=response) as request:
            with self.assertRaises(RuntimeError) as context:
                qbo_upload.get_or_create_item_id(
                    "CANONICAL", object(), "realm", UploadConfig(), {}
                )
        self.assertEqual(request.call_count, 1)
        self.assertIn("does not exist", str(context.exception))

    def test_conversion_preflight_resolves_existing_non_inventory_only(self):
        response = FakeResponse([{"Id": "77", "Name": "CANONICAL", "Type": "NonInventory"}])
        resolved = {}
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=response):
            stats = qbo_upload.resolve_conversion_items(
                ["CANONICAL"], UploadConfig(), object(), "realm", resolved
            )

        self.assertEqual(stats, {"resolved": 1})
        self.assertEqual(resolved["CANONICAL"]["item_id"], "77")
        self.assertFalse(resolved["CANONICAL"]["created"])

    def test_catalog_plan_blocks_legacy_inventory_name_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [approved_row()]))
            plan = build_qbo_catalog_plan(
                registry,
                [{
                    "Product/Service Name": "BACKWOODS DARK STOUT CIGAR EACH",
                    "SKU": "",
                    "Item type": "Inventory",
                }],
            )
        self.assertEqual(plan[0]["Status"], "BLOCK_INVENTORY_NAME_COLLISION")

    def test_catalog_plan_never_creates_missing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [approved_row()]))
            plan = build_qbo_catalog_plan(registry, [])
        self.assertEqual(plan[0]["Status"], "CREATE_NONINVENTORY_PENDING_APPROVAL")

    def test_transform_refuses_conversion_without_mapping_file(self):
        class BlankFileConfig(Config):
            def __init__(self):
                self.product_conversion_file = None

        with self.assertRaises(ValueError) as context:
            transform_dataframe_unified(sales_frame(), BlankFileConfig())
        self.assertIn("file is blank", str(context.exception))

    def test_transform_refuses_conversion_without_catch_all_name(self):
        class NoCatchAllConfig(Config):
            product_conversion_catch_all_name = ""

            def __init__(self, mapping: Path):
                self.product_conversion_file = mapping

        with tempfile.TemporaryDirectory() as tmp:
            mapping = write_mapping(Path(tmp), [approved_row()])
            with self.assertRaises(ValueError) as context:
                transform_dataframe_unified(sales_frame(), NoCatchAllConfig(mapping))
        self.assertIn("catch_all_qbo_name is blank", str(context.exception))

    def test_create_inventory_item_refused_when_conversion_enabled(self):
        with self.assertRaises(RuntimeError) as context:
            qbo_upload.create_inventory_item(
                "CANONICAL",
                "GROCERY",
                10.0,
                5.0,
                UploadConfig(),
                object(),
                "realm",
                {},
                {},
            )
        self.assertIn("product conversion is enabled", str(context.exception))

    def test_create_inventory_item_refused_for_company_a(self):
        class CompanyAConfig:
            product_conversion_enabled = False
            company_key = "company_a"

        with self.assertRaises(RuntimeError) as context:
            qbo_upload.create_inventory_item(
                "CANONICAL",
                "GROCERY",
                10.0,
                5.0,
                CompanyAConfig(),
                object(),
                "realm",
                {},
                {},
            )
        self.assertIn("Company A Inventory item create is disabled", str(context.exception))

    def test_conversion_mode_rejects_blank_item_name(self):
        with self.assertRaises(RuntimeError) as context:
            qbo_upload.get_or_create_item_id("", object(), "realm", UploadConfig(), {})
        self.assertIn("Blank item name", str(context.exception))

    def test_inventory_target_type_is_accepted_on_approved_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = approved_row(**{"Target QBO Item Type": "Inventory", "Target QBO Item Id": "9001"})
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [row]))
        self.assertEqual(registry.rules[0].target_qbo_type, "Inventory")
        self.assertEqual(registry.approved_target_item_ids, {"9001"})

    def test_oct_unmapped_sales_fail_closed_without_catch_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [approved_row()]))
            frame = sales_frame(Product="UNKNOWN OCT SKU", **{"Date/Time": "2026-10-01 10:00:00"})
            with self.assertRaises(ProductResolutionError) as context:
                apply_product_conversion_to_sales(
                    frame,
                    registry,
                    catch_all_name="AKP-UNMAPPED-EPOS-SALES",
                    fail_closed_from=date(2026, 10, 1),
                )
        self.assertIn("UNMAPPED_PRODUCT", str(context.exception))

    def test_sep_unmapped_sales_still_use_catch_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [approved_row()]))
            frame = sales_frame(Product="UNKNOWN SEP SKU", **{"Date/Time": "2026-09-20 10:00:00"})
            result = apply_product_conversion_to_sales(
                frame,
                registry,
                catch_all_name="AKP-UNMAPPED-EPOS-SALES",
                fail_closed_from=date(2026, 10, 1),
            )
        self.assertEqual(result.loc[0, "Product"], "AKP-UNMAPPED-EPOS-SALES")
        self.assertTrue(bool(result.loc[0, "_Product Conversion Fallback"]))

    def test_transform_oct_unmapped_fails_closed_when_configured(self):
        class OctConfig(Config):
            product_conversion_fail_closed_from = date(2026, 10, 1)

            def __init__(self, mapping: Path):
                self.product_conversion_file = mapping

        with tempfile.TemporaryDirectory() as tmp:
            mapping = write_mapping(Path(tmp), [approved_row()])
            with self.assertRaises(ProductResolutionError) as context:
                transform_dataframe_unified(
                    sales_frame(Product="UNKNOWN OCT SKU", **{"Date/Time": "2026-10-01 10:00:00"}),
                    OctConfig(mapping),
                )
        self.assertIn("UNMAPPED_PRODUCT", str(context.exception))

    def test_uploader_accepts_inventory_when_id_is_approved(self):
        class ApprovedInventoryConfig(UploadConfig):
            product_conversion_approved_item_ids = {"9001"}

        response = FakeResponse([{"Id": "9001", "Name": "CANONICAL", "Type": "Inventory"}])
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=response):
            result = qbo_upload.get_or_create_item_id(
                "CANONICAL", object(), "realm", ApprovedInventoryConfig(), {}
            )
        self.assertEqual(result, ("9001", False, "existing_inventory", None))

    def test_resolve_conversion_items_accepts_approved_inventory_id(self):
        class ApprovedInventoryConfig(UploadConfig):
            product_conversion_approved_item_ids = {"9001"}

        response = FakeResponse([{"Id": "9001", "Name": "CANONICAL", "Type": "Inventory"}])
        resolved = {}
        with mock.patch.object(qbo_upload, "_make_qbo_request", return_value=response):
            stats = qbo_upload.resolve_conversion_items(
                ["CANONICAL"], ApprovedInventoryConfig(), object(), "realm", resolved
            )
        self.assertEqual(stats, {"resolved": 1})
        self.assertEqual(resolved["CANONICAL"]["item_id"], "9001")
        self.assertEqual(resolved["CANONICAL"]["type_label"], "existing_inventory")

    def test_catalog_plan_ready_when_inventory_id_matches_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = approved_row(**{"Target QBO Item Type": "Inventory", "Target QBO Item Id": "9001"})
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [row]))
            plan = build_qbo_catalog_plan(
                registry,
                [{
                    "Product/Service Name": "BACKWOODS DARK STOUT CIGAR EACH",
                    "SKU": "AKP-BW-DARK-EACH",
                    "Item type": "Inventory",
                    "Id": "9001",
                }],
            )
        self.assertEqual(plan[0]["Status"], "READY_EXISTING")

    def test_catalog_plan_blocks_legacy_inventory_without_approved_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = approved_row(**{"Target QBO Item Type": "Inventory"})
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [row]))
            plan = build_qbo_catalog_plan(
                registry,
                [{
                    "Product/Service Name": "BACKWOODS DARK STOUT CIGAR EACH",
                    "SKU": "AKP-BW-DARK-EACH",
                    "Item type": "Inventory",
                    "Id": "88",
                }],
            )
        self.assertEqual(plan[0]["Status"], "BLOCK_LEGACY_INVENTORY_NAME_COLLISION")

    def test_catalog_plan_marks_missing_inventory_as_pending_create_not_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = approved_row(**{"Target QBO Item Type": "Inventory"})
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), [row]))
            plan = build_qbo_catalog_plan(registry, [])
        self.assertEqual(plan[0]["Status"], "CREATE_INVENTORY_PENDING_APPROVAL")


if __name__ == "__main__":
    unittest.main()
