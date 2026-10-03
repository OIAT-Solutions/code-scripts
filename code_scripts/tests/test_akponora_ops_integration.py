"""Pipeline hook and cron runner for the company_a operations jobs (no network)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

from code_scripts import run_pipeline
from code_scripts.akponora_ops import common, ops_scheduler
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


class OpsSchedulerTests(unittest.TestCase):
    def setUp(self):
        keys = [k for k in os.environ if k.startswith("OIAT_AKPONORA_")]
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in keys:
            os.environ.pop(key)

    def test_no_job_runs_without_a_cron(self):
        self.assertEqual(ops_scheduler.configured_jobs(), [])

    def test_cron_enables_a_job_and_cmd_overrides(self):
        os.environ["OIAT_AKPONORA_ITEM_GUARD_CRON"] = "0 8 * * *"
        os.environ["OIAT_AKPONORA_ITEM_GUARD_CMD"] = "echo guard"
        jobs = ops_scheduler.configured_jobs()
        self.assertEqual([j.name for j in jobs], ["item_guard"])
        self.assertEqual(jobs[0].command(), ["echo", "guard"])

    def test_default_command_uses_the_module(self):
        cmd = ops_scheduler.JOBS["catalogue_sync"].command()
        self.assertEqual(cmd[:3], [sys.executable, "-m", "code_scripts.akponora_ops.catalogue_sync"])

    def test_writing_job_is_skipped_while_the_run_lock_is_held(self):
        @contextmanager
        def held(_holder):
            yield SimpleNamespace(acquired=False, reason="sales run")

        with mock.patch.object(ops_scheduler, "hold_global_lock", held), \
                mock.patch.object(ops_scheduler.subprocess, "run") as run:
            self.assertEqual(ops_scheduler.run_job(ops_scheduler.JOBS["bills_sync"]), 2)
        run.assert_not_called()

    def test_loop_runs_due_jobs_in_order(self):
        os.environ["OIAT_AKPONORA_ITEM_GUARD_CRON"] = "0 8 * * *"
        os.environ["OIAT_AKPONORA_CATALOGUE_SYNC_CRON"] = "30 6 * * *"
        tz = ZoneInfo("Africa/Lagos")
        clock = [datetime(2026, 10, 2, 6, 0, tzinfo=tz)]
        ran = []

        def fake_sleep(seconds):
            clock[0] += timedelta(seconds=seconds)

        with mock.patch.object(ops_scheduler, "run_job", side_effect=lambda job: ran.append(job.name)):
            ops_scheduler.run_scheduler(sleep=fake_sleep, now=lambda: clock[0], max_cycles=200)
        self.assertEqual(ran[:2], ["catalogue_sync", "item_guard"])

    def test_failed_job_alerts_slack(self):
        with mock.patch.object(ops_scheduler.subprocess, "run", return_value=SimpleNamespace(returncode=1)), \
                mock.patch.object(ops_scheduler, "send_slack") as slack:
            self.assertEqual(ops_scheduler.run_job(ops_scheduler.JOBS["item_guard"]), 1)
        slack.assert_called_once()


if __name__ == "__main__":
    unittest.main()
