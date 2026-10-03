"""Stale 'running' RunJob detection (3 Oct 2026: a Company B job stuck since 21 Aug with a
reused Docker PID blocked every Company B schedule for six weeks)."""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.epos_qbo.models import RunJob, RunLock, RunSchedule, RunScheduleEvent
from apps.epos_qbo.services import run_reconciler, schedule_worker
from apps.epos_qbo.services.run_reconciler import reconcile_stale_running_jobs
from code_scripts.run_lock import GlobalRunLock

HEADLINE = "Marked failed: the run stopped without reporting back (detected "


class _LockFixture(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.lock_path = self.tmp / "lock"
        for patcher in (
            mock.patch("code_scripts.run_lock.lock_file_path", lambda: self.lock_path),
            # Portable default: no /proc start-time evidence unless a test supplies it.
            mock.patch.object(run_reconciler, "process_start_time", return_value=None),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("OIAT_RUN_LOCK_HELD", None)
        os.environ.pop("OIAT_RUNJOB_MAX_HOURS", None)
        self.now = timezone.now()

    def hold_lock(self):
        lock = GlobalRunLock("run_pipeline:company_b:test")
        self.assertTrue(lock.acquire().acquired)
        self.addCleanup(lock.release)
        return lock

    def running_job(self, *, started_ago=timedelta(hours=1), pid=None, **kwargs):
        defaults = {
            "scope": RunJob.SCOPE_SINGLE,
            "company_key": "company_b",
            "status": RunJob.STATUS_RUNNING,
            "pid": os.getpid() if pid is None else pid,  # a PID that certainly exists
            "started_at": self.now - started_ago,
        }
        defaults.update(kwargs)
        return RunJob.objects.create(**defaults)


class ReconcilerTests(_LockFixture):
    def test_reused_pid_with_free_lock_is_reconciled(self):
        job = self.running_job()
        RunLock.objects.create(id=1, active=True, holder=f"dashboard:{job.id}", owner_run_job=job, acquired_at=job.started_at)

        closed = reconcile_stale_running_jobs(now=self.now)

        self.assertEqual([j.id for j in closed], [job.id])
        job.refresh_from_db()
        self.assertEqual(job.status, RunJob.STATUS_FAILED)
        self.assertEqual(job.exit_code, -1)
        self.assertEqual(job.finished_at, self.now)
        self.assertTrue(job.failure_reason.startswith(HEADLINE), job.failure_reason)
        self.assertIn("global run lock was free", job.failure_reason)
        self.assertFalse(RunLock.objects.get(pk=1).active)
        event = RunScheduleEvent.objects.get(run_job=job, event_type=RunScheduleEvent.TYPE_RUN_FAILED)
        self.assertEqual(event.message, job.failure_reason)
        self.assertTrue(event.payload_json["reconciled"])

    def test_genuinely_running_job_with_lock_held_is_left_alone(self):
        self.hold_lock()
        job = self.running_job()

        self.assertEqual(reconcile_stale_running_jobs(now=self.now), [])

        job.refresh_from_db()
        self.assertEqual(job.status, RunJob.STATUS_RUNNING)
        self.assertFalse(RunScheduleEvent.objects.exists())

    def test_fresh_job_gets_grace_before_lock_is_trusted(self):
        job = self.running_job(started_ago=timedelta(seconds=30))
        self.assertEqual(reconcile_stale_running_jobs(now=self.now), [])
        job.refresh_from_db()
        self.assertEqual(job.status, RunJob.STATUS_RUNNING)

    def test_reused_pid_detected_by_process_start_time_while_lock_held_elsewhere(self):
        self.hold_lock()  # e.g. the akponora-ops daily run holds the lock
        job = self.running_job(started_ago=timedelta(hours=2))
        with mock.patch.object(run_reconciler, "process_start_time", return_value=self.now - timedelta(minutes=5)):
            closed = reconcile_stale_running_jobs(now=self.now)
        self.assertEqual(len(closed), 1)
        job.refresh_from_db()
        self.assertIn("belongs to a different process", job.failure_reason)

    def test_max_hours_reconciles_even_when_everything_looks_alive(self):
        self.hold_lock()
        job = self.running_job(started_ago=timedelta(hours=7))

        closed = reconcile_stale_running_jobs(now=self.now)

        self.assertEqual(len(closed), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, RunJob.STATUS_FAILED)
        self.assertTrue(job.failure_reason.startswith(HEADLINE))
        self.assertIn("6-hour safety limit (OIAT_RUNJOB_MAX_HOURS)", job.failure_reason)

    def test_max_hours_is_configurable(self):
        self.hold_lock()
        job = self.running_job(started_ago=timedelta(hours=3))
        os.environ["OIAT_RUNJOB_MAX_HOURS"] = "2"
        self.assertEqual(len(reconcile_stale_running_jobs(now=self.now)), 1)
        job.refresh_from_db()
        self.assertIn("2-hour safety limit", job.failure_reason)

    def test_inventory_job_with_dead_pid_but_active_log_is_left_alone(self):
        # Inventory scopes do not always hold the flock, and a PID checked from another
        # container looks dead; a log still being written means the run is alive.
        log = self.tmp / "run.log"
        log.write_text("working\n")
        job = self.running_job(scope=RunJob.SCOPE_INVENTORY_SYNC, pid=999999, log_file_path=str(log))
        self.assertEqual(reconcile_stale_running_jobs(now=self.now), [])
        job.refresh_from_db()
        self.assertEqual(job.status, RunJob.STATUS_RUNNING)

    def test_inventory_job_with_dead_pid_and_quiet_log_is_reconciled(self):
        log = self.tmp / "run.log"
        log.write_text("old\n")
        old = (self.now - timedelta(hours=1)).timestamp()
        os.utime(log, (old, old))
        job = self.running_job(scope=RunJob.SCOPE_INVENTORY_SYNC, pid=999999, log_file_path=str(log))
        self.assertEqual(len(reconcile_stale_running_jobs(now=self.now)), 1)
        job.refresh_from_db()
        self.assertIn("PID 999999", job.failure_reason)

    def test_management_command_reports_reconciled_jobs(self):
        job = self.running_job()
        call_command("reconcile_run_jobs", stdout=open(os.devnull, "w"))
        job.refresh_from_db()
        self.assertEqual(job.status, RunJob.STATUS_FAILED)


@mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 10, 2))
@mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
class WorkerReconciliationTests(_LockFixture):
    def setUp(self):
        super().setUp()
        os.environ["OIAT_SCHEDULER_ENABLE_ENV_FALLBACK"] = "0"
        self.schedule = RunSchedule.objects.create(
            name="Company B daily",
            enabled=True,
            scope=RunJob.SCOPE_SINGLE,
            company_key="company_b",
            cron_expr="0 18 * * *",
            timezone_name="Africa/Lagos",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            next_fire_at=self.now - timedelta(minutes=1),
        )

    def test_worker_reconciles_stuck_run_then_queues_instead_of_skipping(self, mock_dispatch, _target):
        stuck = self.running_job(scheduled_by=self.schedule, pid=49)
        RunLock.objects.create(id=1, active=True, holder=f"dashboard:{stuck.id}", owner_run_job=stuck, acquired_at=stuck.started_at)

        with mock.patch.object(run_reconciler, "_pid_alive", return_value=True):  # pid 49 reused
            stats = schedule_worker.process_schedule_cycle(now=self.now)

        self.assertEqual(stats["reconciled"], 1)
        self.assertEqual(stats["skipped_overlap"], 0)
        self.assertEqual(stats["queued"], 1)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, RunJob.STATUS_FAILED)
        self.assertTrue(RunJob.objects.filter(scheduled_by=self.schedule, status=RunJob.STATUS_QUEUED).exists())
        self.assertFalse(RunLock.objects.get(pk=1).active)
        self.assertTrue(
            RunScheduleEvent.objects.filter(
                schedule=self.schedule, run_job=stuck, event_type=RunScheduleEvent.TYPE_RUN_FAILED
            ).exists()
        )
        mock_dispatch.assert_called()

    def test_skip_event_names_blocking_run_and_duration(self, _dispatch, _target):
        self.hold_lock()
        active = self.running_job(scheduled_by=self.schedule, started_ago=timedelta(hours=2, minutes=5))

        stats = schedule_worker.process_schedule_cycle(now=self.now)

        self.assertEqual(stats["reconciled"], 0)
        self.assertEqual(stats["skipped_overlap"], 1)
        event = RunScheduleEvent.objects.get(schedule=self.schedule, event_type=RunScheduleEvent.TYPE_SKIPPED_OVERLAP)
        self.assertTrue(event.message.startswith("Skipped because another run is active: "))
        self.assertIn("Single Company (company_b)", event.message)
        self.assertIn(active.friendly_id, event.message)
        self.assertIn("running for 2h 5m", event.message)
        self.assertIn(str(active.id), event.message)
        self.assertEqual(event.payload_json["blocking_run_id"], str(active.id))
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.last_error, f"Blocked by {active.friendly_id} (company_b), running for 2h 5m")

    def test_manual_run_now_reconciles_first(self, _dispatch, _target):
        stuck = self.running_job(scheduled_by=self.schedule)
        job, result = schedule_worker.enqueue_run_for_schedule(self.schedule, now=self.now, source="manual")
        self.assertEqual(result, RunScheduleEvent.TYPE_QUEUED)
        self.assertIsNotNone(job)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, RunJob.STATUS_FAILED)

    def test_reconciler_error_does_not_break_the_cycle(self, _dispatch, _target):
        with mock.patch.object(schedule_worker, "reconcile_stale_running_jobs", side_effect=RuntimeError("boom")):
            stats = schedule_worker.process_schedule_cycle(now=self.now)
        self.assertEqual(stats["reconciled"], 0)
        self.assertEqual(stats["queued"], 1)


class ProcStartTimeTests(TestCase):
    def test_parses_linux_proc_stat(self):
        # comm contains spaces and ')' to exercise the rindex parsing; starttime = 500 ticks.
        stat = "49 (python (x) y) S " + " ".join(["0"] * 18) + " 500 0 0\n"
        files = {"/proc/49/stat": stat, "/proc/stat": "cpu 1 2 3\nbtime 1790000000\n"}
        with mock.patch.object(run_reconciler.Path, "read_text", lambda self: files[str(self)]), \
                mock.patch.object(run_reconciler.os, "sysconf", return_value=100):
            started = run_reconciler.process_start_time(49)
        self.assertEqual(started.timestamp(), 1790000005)

    def test_returns_none_without_proc(self):
        with mock.patch.object(run_reconciler.Path, "read_text", side_effect=FileNotFoundError):
            self.assertIsNone(run_reconciler.process_start_time(49))
