"""Smoke tests for the kept code_scripts/scripts/akponora_cutover tools (offline; no QBO/EPOS calls)."""
import importlib
import unittest
from datetime import datetime

PKG = "code_scripts.scripts.akponora_cutover"
TOOLS = [
    "epos_catalogue_pull", "build_final_mapping", "w5_legacy_rename", "bills_from_epos_pos", "epos_master_links",
    "post_journal", "vendor_admin", "w7_create_items",
]


def mod(name):
    return importlib.import_module(f"{PKG}.{name}")


class ImportAndHelpers(unittest.TestCase):
    def test_every_tool_imports_and_has_main(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                m = mod(name)
                self.assertTrue(callable(getattr(m, "main", None)), name)
                self.assertTrue((m.__doc__ or "").strip(), name)

    def test_business_date_cutoff(self):
        c = mod("_common")
        self.assertEqual(c.business_date(datetime(2026, 9, 17, 4, 59)).isoformat(), "2026-09-16")
        self.assertEqual(c.business_date(datetime(2026, 9, 17, 5, 0)).isoformat(), "2026-09-17")

    def test_catalogue_dedupe_prefers_unfiltered_order(self):
        pull = mod("epos_catalogue_pull")
        caps = [{"url": "page=1&search=X", "body": {"Data": [{"Id": 3}]}},
                {"url": "page=1", "body": {"Data": [{"Id": 1}, {"Id": 2}]}},
                {"url": "page=2", "body": {"Data": [{"Id": 2}, {"Id": 3}]}}]
        self.assertEqual([p["Id"] for p in pull.products_from_captures(caps)], [1, 2, 3])

    def test_w5_name_rules(self):
        w5 = mod("w5_legacy_rename")
        self.assertTrue(w5.valid_new_name("LEGACY — MILK 1L"))
        self.assertFalse(w5.valid_new_name("MILK 1L"))
        self.assertFalse(w5.valid_new_name("LEGACY — A:B"))

    def test_bills_note_parsing(self):
        b = mod("bills_from_epos_pos")
        self.assertEqual(b.parse_note("SUPPLIER:ADEBAM VENTURES   MODE OF PAYMENT:TRANSFER")[:2],
                         ("ADEBAM VENTURES", "TRANSFER"))


class MissingChildMultiplier(unittest.TestCase):
    def test_blank_or_zero_child_multiplier_fails_closed(self):
        final = mod("build_final_mapping")
        for missing in (None, 0, "", "0"):
            with self.subTest(volume_of_sale=missing):
                with self.assertRaisesRegex(ValueError, "Master Product amount"):
                    final.require_child_multiplier("child-1", {"VolumeOfSale": missing})
        # A staged child multiplier of 1 cannot override missing catalogue evidence.
        staged_child = {"VolumeOfSale": None, "Staff Approved Sale Multiplier": "1"}
        with self.assertRaisesRegex(ValueError, "blank/zero VolumeOfSale"):
            final.require_child_multiplier("child-1", staged_child)
        self.assertEqual(final.require_child_multiplier("child-1", {"VolumeOfSale": 4}), 4)


if __name__ == "__main__":
    unittest.main()
