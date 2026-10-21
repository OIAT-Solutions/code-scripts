"""Pipeline hook and cron runner for the company_a operations jobs (no network)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from code_scripts import run_pipeline
from code_scripts.akponora_ops import common
from code_scripts.tests.test_product_conversion import approved_row, write_mapping


class CatalogueSyncHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        mapping = write_mapping(self.root, [approved_row(**{
            "EPOS Product ID": "4242", "EPOS Name": "Wine", "Target QBO Item Type": "Inventory",
            "Target QBO Name": "Wine", "Target QBO SKU": "AKP-4242", "Target QBO Item Id": "90001",
            "Effective Date": "2026-10-01",
        })])
        self.config = SimpleNamespace(product_conversion_enabled=True, product_conversion_file=mapping)
        self.raw = self.root / "raw.csv"
        pd.DataFrame({"Product": ["Wine", "New thing", "Other new", "Blank"],
                      "ProductId": ["4242", "777", "778", ""]}).to_csv(self.raw, index=False)
        env = mock.patch.dict(os.environ, {common.OUTPUT_ROOT_ENV: str(self.root / "runs")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(run_pipeline.CATALOGUE_SYNC_BEFORE_SALES_ENV, None)

    def test_unmapped_ids_are_found_by_product_id(self):
        self.assertEqual(run_pipeline.unmapped_raw_product_ids(str(self.raw), self.config), {"777", "778"})

    def test_hook_is_off_by_default(self):
        with mock.patch("code_scripts.akponora_ops.catalogue_sync.ensure_products_mapped") as ensure:
            run_pipeline.catalogue_sync_before_transform("company_a", "2026-10-02", self.config, str(self.raw))
        ensure.assert_not_called()

    def test_hook_only_applies_to_company_a(self):
        os.environ[run_pipeline.CATALOGUE_SYNC_BEFORE_SALES_ENV] = "1"
        with mock.patch("code_scripts.akponora_ops.catalogue_sync.ensure_products_mapped") as ensure:
            run_pipeline.catalogue_sync_before_transform("company_b", "2026-10-02", self.config, str(self.raw))
        ensure.assert_not_called()

    def test_hook_passes_only_unmapped_ids(self):
        os.environ[run_pipeline.CATALOGUE_SYNC_BEFORE_SALES_ENV] = "1"
        with mock.patch("code_scripts.akponora_ops.catalogue_sync.ensure_products_mapped",
                        return_value={"unresolved": []}) as ensure:
            run_pipeline.catalogue_sync_before_transform("company_a", "2026-10-02", self.config, str(self.raw))
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.args[0], {"777", "778"})

    def test_hook_never_raises(self):
        os.environ[run_pipeline.CATALOGUE_SYNC_BEFORE_SALES_ENV] = "1"
        with mock.patch("code_scripts.akponora_ops.catalogue_sync.ensure_products_mapped",
                        side_effect=RuntimeError("EPOS down")):
            with self.assertLogs(level="ERROR"):
                run_pipeline.catalogue_sync_before_transform("company_a", "2026-10-02", self.config, str(self.raw))


if __name__ == "__main__":
    unittest.main()
