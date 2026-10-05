"""Credit Sales sheet layout (pure plan; no Google calls)."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from code_scripts.akponora_ops import credit_sheet as cs


def fresh_meta():
    return {"sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]}


class CreditSheetTests(unittest.TestCase):
    def test_fresh_sheet_gets_all_tabs_headers_rules_and_products(self):
        plan = cs.build_plan(fresh_meta(), lambda rng: [], products=[["EGG 30PCS [1610049]", "1610049", "EGG 30PCS", 6000.0,
                                                                         "PROVISIONS AND CEREALS", "yes", "2026-10-05"]])
        kinds = [list(r)[0] for r in plan.requests]
        self.assertEqual(plan.requests[0]["updateSheetProperties"]["properties"]["title"], "Invoices")  # Sheet1 renamed
        self.assertEqual(sorted(r["addSheet"]["properties"]["title"] for r in plan.requests if "addSheet" in r),
                         ["Customer prices", "Customers", "Products", "Read me", "Repayments"])
        self.assertIn("addConditionalFormatRule", kinds)
        self.assertIn("addProtectedRange", kinds)
        headers = {v["range"]: v["values"][0] for v in plan.values if v["range"].endswith("!A1")}
        inv = headers["'Invoices'!A1"]
        self.assertEqual(inv[0], "Invoice No")
        self.assertTrue(inv[6].startswith("=ARRAYFORMULA(") and '"List price"' in inv[6])  # computed header cell
        self.assertTrue(inv[10].startswith("=ARRAYFORMULA(") and '"Line total"' in inv[10])
        self.assertIn("'Customers'!A2", [v["range"] for v in plan.values])  # seeded
        prod = next(v for v in plan.values if v["range"] == "'Products'!A2")
        self.assertEqual(prod["values"][0][0], "EGG 30PCS [1610049]")
        dropdowns = [r["setDataValidation"]["rule"]["condition"] for r in plan.requests if "setDataValidation" in r]
        self.assertIn({"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": "=Products!$A$2:$A"}]}, dropdowns)

    def test_sheet1_with_content_is_not_renamed(self):
        with self.assertRaises(SystemExit):
            cs.build_plan(fresh_meta(), lambda rng: [["something typed"]] if "Sheet1" in rng else [], products=[])

    def test_rerun_keeps_people_data_and_refuses_a_hand_edited_header(self):
        meta = {"sheets": [{"properties": {"sheetId": i, "title": t}, "protectedRanges": [{}], "conditionalFormats": [{}]}
                           for i, t in enumerate(cs.ORDER)]}

        def read(rng):
            if rng.endswith("1:1"):
                tab = rng.split("'")[1]
                return [list(cs.TABS[tab])]
            return [["GPFH"]]  # Customers and Invoices already have rows

        plan = cs.build_plan(meta, read, products=[], examples=[["0006242"]])
        ranges = [v["range"] for v in plan.values]
        self.assertNotIn("'Customers'!A2", ranges)
        self.assertNotIn("'Invoices'!A2", ranges)
        self.assertFalse([r for r in plan.requests if "addSheet" in r or "addProtectedRange" in r
                          or "addConditionalFormatRule" in r])

        def edited(rng):
            return [["Invoice No", "Date", "Client"]] if rng == "'Invoices'!1:1" else read(rng)

        with self.assertRaises(SystemExit):
            cs.build_plan(meta, edited, products=[])

    def test_example_rows_only_take_invoices_not_in_quickbooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.csv"
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Invoice No", "Date", "Customer (as written)", "Address", "Paid mark", "Qty",
                            "Description (as written)", "Unit price", "Amount", "Invoice total", "In QBO already"])
                w.writerow(["0006224", "2026-09-17", "GOLD PLATE", "CTH-HQ", "Not Paid", "6", "Rice", "63000", "378000", "2074000", "SR-20260408-0023"])
                w.writerow(["0006239", "2026-09-29", "GOLD PLATE", "IKD", "Not Paid", "20", "Vegetable oil", "55000", "1100000", "1100000", "no"])
            rows = cs.example_rows(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:4], ["0006239", "2026-09-29", "GPFH", "Ikorodu / Ayangbure (IKD)"])
        self.assertIs(rows[0][12], False)  # never Ready
        self.assertIn("Vegetable oil", rows[0][14])


if __name__ == "__main__":
    unittest.main()
