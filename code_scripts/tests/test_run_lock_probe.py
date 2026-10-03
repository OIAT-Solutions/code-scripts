"""global_lock_is_free: the non-blocking probe used to detect stale portal runs."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from code_scripts import run_lock


@unittest.skipIf(os.name == "nt", "flock probe is POSIX-only")
class GlobalLockProbeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock_path = Path(tmp.name) / "lock"
        p = mock.patch("code_scripts.run_lock.lock_file_path", lambda: self.lock_path)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(run_lock.LOCK_HELD_ENV, None)

    def test_free_when_nobody_holds_it(self):
        self.assertIs(run_lock.global_lock_is_free(), True)

    def test_busy_while_held_and_free_after_release(self):
        lock = run_lock.GlobalRunLock("run_pipeline:company_b:2026-10-02")
        self.assertTrue(lock.acquire().acquired)
        try:
            self.assertIs(run_lock.global_lock_is_free(), False)
        finally:
            lock.release()
        self.assertIs(run_lock.global_lock_is_free(), True)

    def test_probe_does_not_rewrite_holder_or_keep_the_lock(self):
        lock = run_lock.GlobalRunLock("run_pipeline:company_b:2026-10-02")
        self.assertTrue(lock.acquire().acquired)
        lock.release()
        before = self.lock_path.read_text(encoding="utf-8")
        self.assertIs(run_lock.global_lock_is_free(), True)
        self.assertEqual(self.lock_path.read_text(encoding="utf-8"), before)
        again = run_lock.GlobalRunLock("next")
        self.assertTrue(again.acquire().acquired)
        again.release()

    def test_probe_ignores_inherited_lock_held_env(self):
        # A child that inherits OIAT_RUN_LOCK_HELD must not make the probe lie.
        os.environ[run_lock.LOCK_HELD_ENV] = "1"
        holder = run_lock.GlobalRunLock("x")
        holder._handle = open(self.lock_path, "a+", encoding="utf-8")  # take the real flock
        import fcntl

        fcntl.flock(holder._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertIs(run_lock.global_lock_is_free(), False)
        finally:
            fcntl.flock(holder._handle.fileno(), fcntl.LOCK_UN)
            holder._handle.close()


if __name__ == "__main__":
    unittest.main()
