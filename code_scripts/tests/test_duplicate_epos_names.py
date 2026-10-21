import tempfile
import unittest
from pathlib import Path

from code_scripts.product_conversion import MappingValidationError, ProductConversionRegistry
from code_scripts.tests.test_product_conversion import approved_row, write_mapping


def rice_rows(second_id="1707170"):
    first = approved_row(**{
        "Row ID": "1", "EPOS Product ID": "1707169", "EPOS Existing SKU": "",
        "EPOS Name": "MY CHOICE RICE 5kg", "Target QBO Name": "MY CHOICE RICE 5kg #1707169",
        "Target QBO SKU": "AKP-1707169", "Canonical Family Key": "AKP-1707169",
    })
    second = approved_row(**{
        "Row ID": "2", "EPOS Product ID": second_id, "EPOS Existing SKU": "",
        "EPOS Name": "MY CHOICE RICE 5kg", "Target QBO Name": "MY CHOICE RICE 5kg #1707170",
        "Target QBO SKU": "AKP-1707170", "Canonical Family Key": "AKP-1707170",
    })
    return [first, second]


class DuplicateEposNameTests(unittest.TestCase):
    def test_duplicate_names_with_product_ids_load_and_resolve_by_id_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ProductConversionRegistry.from_csv(write_mapping(Path(tmp), rice_rows()))
        self.assertEqual(registry.by_product_id["1707169"].target_qbo_sku, "AKP-1707169")
        self.assertEqual(registry.by_product_id["1707170"].target_qbo_sku, "AKP-1707170")
        self.assertNotIn("my choice rice 5kg", registry.by_name)

    def test_duplicate_names_without_product_id_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(MappingValidationError, "duplicate EPOS Name"):
                ProductConversionRegistry.from_csv(write_mapping(Path(tmp), rice_rows(second_id="")))


if __name__ == "__main__":
    unittest.main()
