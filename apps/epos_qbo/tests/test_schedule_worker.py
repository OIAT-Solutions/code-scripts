from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.epos_qbo.models import RunJob, RunSchedule, RunScheduleEvent, SchedulerWorkerHeartbeat
from apps.epos_qbo.services import schedule_worker


class ScheduleWorkerTests(TestCase):
    def setUp(self):
        self.fixed_now = timezone.make_aware(datetime(2026, 2, 20, 10, 0, 0))

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_due_schedule_queues_run_job(self, _mock_target_date, _mock_dispatch):
        schedule = RunSchedule.objects.create(
            name="Daily all companies",
            enabled=True,
            scope=RunJob.SCOPE_ALL,
            cron_expr="*/5 * * * *",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            parallel=2,
            stagger_seconds=2,
            continue_on_failure=False,
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )

        stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["due"], 1)
        self.assertEqual(stats["queued"], 1)
        queued_jobs = RunJob.objects.filter(scheduled_by=schedule)
        self.assertEqual(queued_jobs.count(), 1)
        queued_job = queued_jobs.first()
        assert queued_job is not None
        self.assertEqual(queued_job.status, RunJob.STATUS_QUEUED)
        self.assertEqual(queued_job.target_date.isoformat(), "2026-02-19")

        schedule.refresh_from_db()
        self.assertEqual(schedule.last_result, RunSchedule.LAST_RESULT_QUEUED)
        self.assertIsNotNone(schedule.next_fire_at)
        self.assertGreater(schedule.next_fire_at, self.fixed_now)
        event = RunScheduleEvent.objects.filter(
            schedule=schedule,
            event_type=RunScheduleEvent.TYPE_QUEUED,
        ).first()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.payload_json.get("schedule_name"), schedule.name)
        self.assertEqual(event.payload_json.get("schedule_id"), str(schedule.id))

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    def test_due_one_time_schedule_queues_once_and_disables(self, _mock_dispatch):
        schedule = RunSchedule.objects.create(
            name="Sunday Goldplates Sales",
            enabled=True,
            schedule_type=RunSchedule.SCHEDULE_TYPE_ONE_TIME,
            scope=RunJob.SCOPE_SINGLE,
            company_key="company_b",
            cron_expr="",
            timezone_name="Africa/Lagos",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            run_once_at=self.fixed_now - timedelta(minutes=1),
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )

        stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["due"], 1)
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(RunJob.objects.filter(scheduled_by=schedule).count(), 1)
        job = RunJob.objects.get(scheduled_by=schedule)
        self.assertEqual((job.scope, job.company_key), (RunJob.SCOPE_SINGLE, "company_b"))
        schedule.refresh_from_db()
        self.assertFalse(schedule.enabled)
        self.assertEqual(schedule.completed_at, self.fixed_now)
        self.assertIsNone(schedule.next_fire_at)
        self.assertEqual(schedule.last_result, RunSchedule.LAST_RESULT_QUEUED)
        self.assertTrue(
            RunScheduleEvent.objects.filter(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_ONE_TIME_COMPLETED,
            ).exists()
        )

        second_stats = schedule_worker.process_schedule_cycle(now=self.fixed_now + timedelta(minutes=1))

        self.assertEqual(second_stats["due"], 0)
        self.assertEqual(RunJob.objects.filter(scheduled_by=schedule).count(), 1)

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    def test_invalid_one_time_schedule_missing_run_once_at_records_invalid(self, _mock_dispatch):
        schedule = RunSchedule.objects.create(
            name="Invalid one-time",
            enabled=True,
            schedule_type=RunSchedule.SCHEDULE_TYPE_ONE_TIME,
            scope=RunJob.SCOPE_ALL,
            cron_expr="",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            run_once_at=None,
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )

        stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["due"], 1)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["skipped_invalid"], 1)
        schedule.refresh_from_db()
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.last_result, RunSchedule.LAST_RESULT_SKIPPED_INVALID)
        self.assertIn("Run once time is required", schedule.last_error)
        self.assertTrue(
            RunScheduleEvent.objects.filter(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
            ).exists()
        )

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_due_schedule_skips_overlap(self, _mock_target_date, _mock_dispatch):
        schedule = RunSchedule.objects.create(
            name="Daily overlap check",
            enabled=True,
            scope=RunJob.SCOPE_ALL,
            cron_expr="*/5 * * * *",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            parallel=2,
            stagger_seconds=2,
            continue_on_failure=False,
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )
        RunJob.objects.create(
            scope=RunJob.SCOPE_ALL,
            status=RunJob.STATUS_RUNNING,
            scheduled_by=schedule,
            target_date=date(2026, 2, 19),
        )

        stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["due"], 1)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["skipped_overlap"], 1)
        self.assertEqual(
            RunJob.objects.filter(scheduled_by=schedule, status=RunJob.STATUS_QUEUED).count(),
            0,
        )
        self.assertTrue(
            RunScheduleEvent.objects.filter(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_SKIPPED_OVERLAP,
            ).exists()
        )

    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_company_a_exclusion_keeps_trading_target_date(self, _mock_target_date):
        # Regression (H2): the exclusion must not swallow the trading-date assignment.
        schedule = RunSchedule(
            name="all",
            enabled=True,
            scope=RunJob.SCOPE_ALL,
            cron_expr="0 18 * * *",
            timezone_name="Africa/Lagos",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
        )
        payload = schedule_worker._job_payload_from_schedule(schedule, now=self.fixed_now)
        self.assertEqual(payload["target_date"], date(2026, 2, 19))
        self.assertEqual(payload["inventory_options_json"], {"exclude_companies": ["company_a"]})

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_user_all_company_schedule_excludes_company_a_in_command(self, _mock_target_date, _mock_dispatch):
        from apps.epos_qbo.services.job_runner import build_command_for_job

        schedule = RunSchedule.objects.create(
            name="User daily all",
            enabled=True,
            scope=RunJob.SCOPE_ALL,
            cron_expr="*/5 * * * *",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )
        schedule_worker.process_schedule_cycle(now=self.fixed_now)
        job = RunJob.objects.get(scheduled_by=schedule)
        self.assertEqual(job.target_date.isoformat(), "2026-02-19")
        command = build_command_for_job(job)
        self.assertEqual(command[command.index("--exclude-company") + 1], "company_a")

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_company_a_single_sales_schedule_never_queues(self, _mock_target_date, _mock_dispatch):
        """Akponora's sales run only inside its Daily routine: there is no switch to allow a sales schedule."""
        schedule = RunSchedule.objects.create(
            name="Company A daily",
            enabled=True,
            scope=RunJob.SCOPE_SINGLE,
            company_key="company_a",
            cron_expr="*/5 * * * *",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )
        job, result = schedule_worker.enqueue_run_for_schedule(schedule, now=self.fixed_now)
        self.assertIsNone(job)
        self.assertEqual(result, RunScheduleEvent.TYPE_SKIPPED_INVALID)
        self.assertFalse(RunJob.objects.filter(scheduled_by=schedule).exists())
        schedule.refresh_from_db()
        self.assertIn("Daily routine", schedule.last_error)

    def test_schedule_outside_the_catalogue_never_queues(self):
        schedule = RunSchedule.objects.create(
            name="Weekly Inventory Sync",
            enabled=True,
            scope=RunJob.SCOPE_INVENTORY_PIPELINE,
            company_key="company_b",
            cron_expr="0 20 * * 0",
            timezone_name="Africa/Lagos",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
        )
        job, result = schedule_worker.enqueue_run_for_schedule(schedule, now=self.fixed_now)
        self.assertIsNone(job)
        self.assertEqual(result, RunScheduleEvent.TYPE_SKIPPED_INVALID)
        schedule.refresh_from_db()
        self.assertIn("workflow catalogue", schedule.last_error)

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_other_company_single_schedule_unaffected(self, _mock_target_date, _mock_dispatch):
        schedule = RunSchedule.objects.create(
            name="Company B daily",
            enabled=True,
            scope=RunJob.SCOPE_SINGLE,
            company_key="company_b",
            cron_expr="*/5 * * * *",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
        )
        job, result = schedule_worker.enqueue_run_for_schedule(schedule, now=self.fixed_now)
        self.assertEqual(result, RunScheduleEvent.TYPE_QUEUED)
        self.assertEqual(job.inventory_options_json, {})

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_process_schedule_cycle_records_heartbeat(self, _mock_target_date, _mock_dispatch):
        RunSchedule.objects.create(
            name="Daily",
            enabled=True,
            scope=RunJob.SCOPE_ALL,
            cron_expr="0 18 * * *",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            next_fire_at=self.fixed_now + timedelta(hours=1),
        )
        schedule_worker.process_schedule_cycle(now=self.fixed_now)
        hb = SchedulerWorkerHeartbeat.objects.filter(id=1).first()
        self.assertIsNotNone(hb)
        self.assertEqual(hb.last_seen, self.fixed_now)

    def test_get_scheduler_status_no_heartbeat_returns_not_running(self):
        SchedulerWorkerHeartbeat.objects.filter(id=1).delete()
        status = schedule_worker.get_scheduler_status()
        self.assertFalse(status["running"])
        self.assertIsNone(status["last_seen"])
        self.assertIn("not run", status["message"])

    def test_get_scheduler_status_recent_heartbeat_returns_running(self):
        now = timezone.now()
        SchedulerWorkerHeartbeat.objects.update_or_create(id=1, defaults={"last_seen": now})
        with mock.patch("apps.epos_qbo.services.schedule_worker.configured_poll_seconds", return_value=15):
            status = schedule_worker.get_scheduler_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["last_seen"], now)
        self.assertIn("Worker is polling", status["message"])


class CompanyAScheduleRulesTests(TestCase):
    """Akponora runs in its Daily routine; other companies' sales schedules are unaffected."""

    def test_other_companies_sales_schedules_still_queue(self):
        schedule = RunSchedule.objects.create(
            name="Company B daily",
            enabled=True,
            scope=RunJob.SCOPE_SINGLE,
            company_key="company_b",
            cron_expr="0 18 * * *",
            timezone_name="Africa/Lagos",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
        )
        with mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job"):
            job, result = schedule_worker.enqueue_run_for_schedule(schedule, now=timezone.now())
        self.assertIsNotNone(job)
        self.assertEqual(job.company_key, "company_b")

    def test_trading_date_before_cutoff_is_two_days_back(self):
        from datetime import timezone as dt_timezone

        from apps.epos_qbo.business_date import get_target_trading_date

        # 04:30 Lagos on 2 Oct: business day 1 Oct is still open -> 30 Sep.
        early = datetime(2026, 10, 2, 3, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(get_target_trading_date(now=early).isoformat(), "2026-09-30")
        # 05:00 Lagos on 2 Oct onwards -> 1 Oct.
        cutoff = datetime(2026, 10, 2, 4, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(get_target_trading_date(now=cutoff).isoformat(), "2026-10-01")


class WorkerLoopTests(TestCase):
    def test_a_locked_database_never_crashes_the_worker(self):
        from io import StringIO

        from django.core.management import call_command
        from django.db import OperationalError

        calls = []

        def cycle():
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError("database is locked")
            raise KeyboardInterrupt  # stop the loop on the second cycle

        err = StringIO()
        with mock.patch("apps.epos_qbo.management.commands.run_schedule_worker.process_schedule_cycle", side_effect=cycle), \
                mock.patch("apps.epos_qbo.management.commands.run_schedule_worker.time.sleep"):
            with self.assertRaises(KeyboardInterrupt):
                call_command("run_schedule_worker", poll_seconds=1, stdout=StringIO(), stderr=err)
        self.assertEqual(len(calls), 2)
        self.assertIn("database busy", err.getvalue())
