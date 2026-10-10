"""Daily clean-up of old working files (services/housekeeping.py)."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

from django.test import SimpleTestCase

from apps.epos_qbo.services import housekeeping

TODAY = date(2026, 10, 5)


class HousekeepingTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="oiat_hk_", dir=os.getenv("TMPDIR") or None))
        self.addCleanup(shutil.rmtree, self.root, True)

    def make(self, rel, days_old=0):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        t = time.time() - days_old * 86400
        os.utime(p, (t, t))
        return p

    def test_keeps_a_month_of_working_files_and_three_months_of_evidence(self):
        old_day = self.make("code_scripts/Uploaded/2026-08-01/raw.csv")
        new_day = self.make("code_scripts/Uploaded/2026-09-20/raw.csv")
        old_log = self.make("code_scripts/logs/pipeline_old.log", days_old=40)
        new_log = self.make("code_scripts/logs/runs/new.log", days_old=5)
        lock = self.make("code_scripts/logs/.oiat_global_run.lock", days_old=400)
        old_spill = self.make("code_scripts/uploads/spill_raw/Co/2026-08-01.csv", days_old=60)
        old_evidence = self.make("ops/company_a/daily/2026-06-01/run_1/summary.json")
        kept_evidence = self.make("ops/company_a/daily/2026-08-01/run_1/summary.json")
        state = self.make("ops/company_a/catalogue_sync/state/catalogue_snapshot.json", days_old=400)
        config = self.make("code_scripts/companies/company_a.json", days_old=400)

        dry = housekeeping.prune(today=TODAY, dry_run=True, state_root=self.root)
        self.assertTrue(old_day.exists())
        self.assertGreater(len(dry.removed), 0)

        res = housekeeping.prune(today=TODAY, state_root=self.root)
        for gone in (old_day, old_log, old_spill, old_evidence):
            self.assertFalse(gone.exists(), gone)
        for kept in (new_day, new_log, lock, kept_evidence, state, config):
            self.assertTrue(kept.exists(), kept)
        self.assertEqual(res.removed_dirs, 2)  # Uploaded/2026-08-01 and the June evidence day
        self.assertFalse((self.root / "code_scripts/uploads/spill_raw/Co").exists())  # empty folder dropped

    def test_runs_once_a_day(self):
        self.assertTrue(housekeeping.due(TODAY, state_root=self.root))
        housekeeping.mark_done(TODAY, housekeeping.Result(), state_root=self.root)
        self.assertFalse(housekeeping.due(TODAY, state_root=self.root))
        self.assertTrue(housekeeping.due(date(2026, 10, 6), state_root=self.root))
