from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.epos_qbo.models import RunJob, RunLock, RunSchedule, RunScheduleEvent, SchedulerWorkerHeartbeat
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
            name="Sunday Inventory Sync",
            enabled=True,
            schedule_type=RunSchedule.SCHEDULE_TYPE_ONE_TIME,
            scope=RunJob.SCOPE_INVENTORY_PIPELINE,
            company_key="company_a",
            cron_expr="",
            timezone_name="Africa/Lagos",
            inventory_options_json={"product_filter": "TROPHY"},
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            run_once_at=self.fixed_now - timedelta(minutes=1),
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )

        with mock.patch.dict("os.environ", {"OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "0"}, clear=False):
            stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["due"], 1)
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(RunJob.objects.filter(scheduled_by=schedule).count(), 1)
        job = RunJob.objects.get(scheduled_by=schedule)
        self.assertEqual(job.scope, RunJob.SCOPE_INVENTORY_PIPELINE)
        self.assertEqual(job.inventory_options_json, {"product_filter": "TROPHY"})
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

        with mock.patch.dict("os.environ", {"OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "0"}, clear=False):
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

        with mock.patch.dict("os.environ", {"OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "0"}, clear=False):
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

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    def test_due_default_inventory_schedule_queues_pipeline_job_without_filters(self, _mock_dispatch):
        schedule = RunSchedule.objects.create(
            name="Weekly Inventory Sync",
            enabled=True,
            scope=RunJob.SCOPE_INVENTORY_PIPELINE,
            company_key="company_a",
            cron_expr="0 20 * * 0",
            timezone_name="Africa/Lagos",
            inventory_options_json={},
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            parallel=2,
            stagger_seconds=2,
            continue_on_failure=True,
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )

        with mock.patch.dict("os.environ", {"OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "0"}, clear=False):
            stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["due"], 1)
        self.assertEqual(stats["queued"], 1)
        job = RunJob.objects.get(scheduled_by=schedule)
        self.assertEqual(job.scope, RunJob.SCOPE_INVENTORY_PIPELINE)
        self.assertEqual(job.company_key, "company_a")
        self.assertIsNone(job.target_date)
        self.assertEqual(job.parallel, 1)
        self.assertFalse(job.continue_on_failure)
        self.assertEqual(job.inventory_options_json, {})
        event = RunScheduleEvent.objects.get(schedule=schedule, event_type=RunScheduleEvent.TYPE_QUEUED)
        self.assertEqual(event.message, "Run queued.")
        self.assertEqual(event.friendly_message, "Run queued")

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    def test_inventory_schedule_skips_when_another_run_is_active(self, _mock_dispatch):
        active_sales = RunJob.objects.create(
            scope=RunJob.SCOPE_ALL,
            status=RunJob.STATUS_RUNNING,
            target_date=date(2026, 2, 19),
        )
        RunLock.objects.create(active=True, holder="dashboard:sales", owner_run_job=active_sales)
        schedule = RunSchedule.objects.create(
            name="Weekly Inventory Sync",
            enabled=True,
            scope=RunJob.SCOPE_INVENTORY_PIPELINE,
            company_key="company_a",
            cron_expr="0 20 * * 0",
            timezone_name="Africa/Lagos",
            inventory_options_json={},
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            next_fire_at=self.fixed_now - timedelta(minutes=1),
        )

        with mock.patch.dict("os.environ", {"OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "0"}, clear=False):
            stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["skipped_overlap"], 1)
        self.assertFalse(RunJob.objects.filter(scheduled_by=schedule).exists())
        event = RunScheduleEvent.objects.get(
            schedule=schedule,
            event_type=RunScheduleEvent.TYPE_SKIPPED_OVERLAP,
        )
        self.assertTrue(event.message.startswith("Skipped because another run is active: "))
        self.assertIn(active_sales.friendly_id, event.message)
        self.assertEqual(event.payload_json.get("blocking_run_id"), str(active_sales.id))
        self.assertEqual(event.friendly_message, "Skipped because another run is active")

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    def test_fallback_schedule_is_created_when_enabled_and_no_user_schedule(self, _mock_dispatch):
        with mock.patch.dict(
            "os.environ",
            {
                "OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "1",
                "SCHEDULE_CRON": "*/7 * * * *",
                "SCHEDULE_TZ": "UTC",
            },
            clear=False,
        ):
            stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["fallback_enabled"], 1)
        fallback = RunSchedule.objects.get(name=schedule_worker.FALLBACK_SCHEDULE_NAME, is_system_managed=True)
        self.assertTrue(fallback.enabled)
        self.assertEqual(fallback.cron_expr, "*/7 * * * *")
        self.assertEqual(fallback.timezone_name, "UTC")
        self.assertIsNotNone(fallback.next_fire_at)

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    def test_fallback_schedule_is_disabled_when_user_schedule_exists(self, _mock_dispatch):
        RunSchedule.objects.create(
            name="User schedule",
            enabled=True,
            scope=RunJob.SCOPE_ALL,
            cron_expr="*/10 * * * *",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            next_fire_at=self.fixed_now + timedelta(minutes=5),
        )
        fallback = RunSchedule.objects.create(
            name=schedule_worker.FALLBACK_SCHEDULE_NAME,
            enabled=True,
            scope=RunJob.SCOPE_ALL,
            cron_expr="*/5 * * * *",
            timezone_name="UTC",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            is_system_managed=True,
            next_fire_at=self.fixed_now + timedelta(minutes=1),
        )

        with mock.patch.dict(
            "os.environ",
            {
                "OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "1",
                "SCHEDULE_CRON": "*/5 * * * *",
                "SCHEDULE_TZ": "UTC",
            },
            clear=False,
        ):
            stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["fallback_disabled"], 1)
        fallback.refresh_from_db()
        self.assertFalse(fallback.enabled)
        self.assertTrue(
            RunScheduleEvent.objects.filter(
                schedule=fallback,
                event_type=RunScheduleEvent.TYPE_FALLBACK_DISABLED,
            ).exists()
        )

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    def test_inventory_schedule_does_not_disable_sales_env_fallback(self, _mock_dispatch):
        RunSchedule.objects.create(
            name="Weekly Inventory Sync",
            enabled=True,
            scope=RunJob.SCOPE_INVENTORY_PIPELINE,
            company_key="company_a",
            cron_expr="0 20 * * 0",
            timezone_name="Africa/Lagos",
            inventory_options_json={},
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            next_fire_at=self.fixed_now + timedelta(days=1),
        )

        with mock.patch.dict(
            "os.environ",
            {
                "OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "1",
                "SCHEDULE_CRON": "0 19 * * *",
                "SCHEDULE_TZ": "Africa/Lagos",
            },
            clear=False,
        ):
            stats = schedule_worker.process_schedule_cycle(now=self.fixed_now)

        self.assertEqual(stats["fallback_enabled"], 1)
        fallback = RunSchedule.objects.get(name=schedule_worker.FALLBACK_SCHEDULE_NAME, is_system_managed=True)
        self.assertTrue(fallback.enabled)
        self.assertEqual(fallback.scope, RunJob.SCOPE_ALL)

    def test_system_fallback_excludes_company_a_until_explicit_opt_in(self):
        fallback = RunSchedule.objects.create(
            name=schedule_worker.FALLBACK_SCHEDULE_NAME,
            enabled=True,
            scope=RunJob.SCOPE_ALL,
            cron_expr="0 18 * * *",
            timezone_name="Africa/Lagos",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            is_system_managed=True,
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            payload = schedule_worker._job_payload_from_schedule(fallback, now=self.fixed_now)
        self.assertEqual(payload["inventory_options_json"]["exclude_companies"], ["company_a"])

        with mock.patch.dict(
            "os.environ", {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "1"}, clear=True
        ):
            opted_in = schedule_worker._job_payload_from_schedule(fallback, now=self.fixed_now)
        self.assertNotIn("exclude_companies", opted_in["inventory_options_json"])

    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_company_a_exclusion_keeps_trading_target_date(self, _mock_target_date):
        # Regression (H2): the exclusion must not swallow the trading-date assignment.
        for system_managed in (True, False):
            with self.subTest(system_managed=system_managed):
                schedule = RunSchedule(
                    name=f"all-{system_managed}",
                    enabled=True,
                    scope=RunJob.SCOPE_ALL,
                    cron_expr="0 18 * * *",
                    timezone_name="Africa/Lagos",
                    target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
                    is_system_managed=system_managed,
                )
                with mock.patch.dict("os.environ", {}, clear=True):
                    payload = schedule_worker._job_payload_from_schedule(schedule, now=self.fixed_now)
                self.assertEqual(payload["target_date"], date(2026, 2, 19))
                self.assertEqual(payload["inventory_options_json"], {"exclude_companies": ["company_a"]})

    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_inventory_pipeline_schedule_has_no_target_date_or_exclusion(self, _mock_target_date):
        schedule = RunSchedule(
            name="inv",
            enabled=True,
            scope=RunJob.SCOPE_INVENTORY_PIPELINE,
            company_key="company_b",
            cron_expr="0 20 * * 0",
            timezone_name="Africa/Lagos",
            inventory_options_json={"mode": "x"},
            is_system_managed=True,
        )
        with mock.patch.dict("os.environ", {}, clear=True):
            payload = schedule_worker._job_payload_from_schedule(schedule, now=self.fixed_now)
        self.assertIsNone(payload["target_date"])
        self.assertEqual(payload["inventory_options_json"], {"mode": "x"})

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
        with mock.patch.dict(
            "os.environ", {"OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "0"}, clear=False
        ), mock.patch.dict("os.environ", {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "0"}):
            schedule_worker.process_schedule_cycle(now=self.fixed_now)
        job = RunJob.objects.get(scheduled_by=schedule)
        self.assertEqual(job.target_date.isoformat(), "2026-02-19")
        command = build_command_for_job(job)
        self.assertEqual(command[command.index("--exclude-company") + 1], "company_a")

    @mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job")
    @mock.patch("apps.epos_qbo.services.schedule_worker.get_target_trading_date", return_value=date(2026, 2, 19))
    def test_company_a_single_sales_schedule_skipped_until_opt_in(self, _mock_target_date, _mock_dispatch):
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
        with mock.patch.dict("os.environ", {"OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "0"}, clear=False):
            os_env = {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "0"}
            with mock.patch.dict("os.environ", os_env):
                job, result = schedule_worker.enqueue_run_for_schedule(schedule, now=self.fixed_now)
            self.assertIsNone(job)
            self.assertEqual(result, RunScheduleEvent.TYPE_SKIPPED_INVALID)
            self.assertFalse(RunJob.objects.filter(scheduled_by=schedule).exists())
            schedule.refresh_from_db()
            self.assertIn("OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED", schedule.last_error)

            with mock.patch.dict("os.environ", {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "1"}):
                job, result = schedule_worker.enqueue_run_for_schedule(schedule, now=self.fixed_now)
            self.assertIsNotNone(job)
            self.assertEqual(job.company_key, "company_a")
            self.assertEqual(job.target_date.isoformat(), "2026-02-19")

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
        with mock.patch.dict("os.environ", {"OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "0"}):
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


class CompanyAStandingApprovalSchedulerTests(TestCase):
    """System fallback at 18:00 Lagos posts the previous business day, Company A included."""

    STANDING_ENV = {
        "OIAT_SCHEDULER_ENABLE_ENV_FALLBACK": "1",
        "SCHEDULE_CRON": "0 18 * * *",
        "SCHEDULE_TZ": "Africa/Lagos",
        "OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED": "1",
        "OIAT_COMPANY_A_STANDING_APPROVAL_REF": "owner standing approval (chat ref)",
    }

    def _fire_fallback_on_2_oct(self, env):
        from datetime import timezone as dt_timezone

        morning = datetime(2026, 10, 2, 8, 0, tzinfo=dt_timezone.utc)  # 09:00 Lagos
        with mock.patch.dict("os.environ", env, clear=False), mock.patch(
            "apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job"
        ):
            schedule_worker.process_schedule_cycle(now=morning)
            schedule = RunSchedule.objects.get(is_system_managed=True)
            # 18:00 Africa/Lagos (UTC+1) == 17:00 UTC.
            self.assertEqual(schedule.next_fire_at, datetime(2026, 10, 2, 17, 0, tzinfo=dt_timezone.utc))
            stats = schedule_worker.process_schedule_cycle(now=schedule.next_fire_at + timedelta(seconds=10))
        self.assertEqual(stats["queued"], 1)
        return RunJob.objects.get(scheduled_by=schedule)

    def test_fallback_on_2_oct_posts_1_oct_including_company_a_and_passes_env(self):
        from apps.epos_qbo.services import job_runner

        job = self._fire_fallback_on_2_oct(self.STANDING_ENV)
        self.assertEqual(job.target_date.isoformat(), "2026-10-01")
        self.assertNotIn("exclude_companies", job.inventory_options_json)
        command = job_runner.build_command_for_job(job)
        self.assertTrue(command[1].endswith("run_all_companies.py"))
        self.assertEqual(command[command.index("--target-date") + 1], "2026-10-01")
        self.assertNotIn("--exclude-company", command)

        popen = mock.MagicMock(pid=4242)
        env = dict(self.STANDING_ENV)
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(job_runner.subprocess, "Popen", return_value=popen) as popen_cls, \
             mock.patch.object(job_runner.threading, "Thread"):
            import os as _os
            _os.environ.pop("COMPANY_A_POSTING_APPROVAL_FILE", None)
            job_runner.start_run_job(job, command)
        child_env = popen_cls.call_args.kwargs["env"]
        # run_pipeline (via run_all_companies) inherits both flags -> standing auto-approval mode.
        self.assertEqual(child_env["OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED"], "1")
        self.assertEqual(child_env["OIAT_COMPANY_A_STANDING_APPROVAL_REF"], "owner standing approval (chat ref)")
        self.assertNotIn("COMPANY_A_POSTING_APPROVAL_FILE", child_env)

    def test_fallback_without_opt_in_still_excludes_company_a(self):
        from apps.epos_qbo.services import job_runner

        env = dict(self.STANDING_ENV, OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED="0")
        job = self._fire_fallback_on_2_oct(env)
        self.assertEqual(job.target_date.isoformat(), "2026-10-01")
        command = job_runner.build_command_for_job(job)
        self.assertEqual(command[command.index("--exclude-company") + 1], "company_a")

    def test_daily_run_owns_company_a_so_fallback_excludes_it(self):
        from apps.epos_qbo.services import job_runner

        env = dict(self.STANDING_ENV, OIAT_COMPANY_A_DAILY_RUN_ENABLED="1")
        job = self._fire_fallback_on_2_oct(env)
        self.assertEqual(job.inventory_options_json.get("exclude_companies"), ["company_a"])
        command = job_runner.build_command_for_job(job)
        self.assertEqual(command[command.index("--exclude-company") + 1], "company_a")

    def test_daily_run_blocks_single_company_a_sales_schedule(self):
        schedule = RunSchedule.objects.create(
            name="Company A daily",
            enabled=True,
            scope=RunJob.SCOPE_SINGLE,
            company_key="company_a",
            cron_expr="0 18 * * *",
            timezone_name="Africa/Lagos",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
        )
        env = dict(self.STANDING_ENV, OIAT_COMPANY_A_DAILY_RUN_ENABLED="1")
        with mock.patch.dict("os.environ", env, clear=False):
            job, result = schedule_worker.enqueue_run_for_schedule(schedule, now=timezone.now())
        self.assertIsNone(job)
        self.assertEqual(result, RunScheduleEvent.TYPE_SKIPPED_INVALID)
        schedule.refresh_from_db()
        self.assertIn("OIAT_COMPANY_A_DAILY_RUN_ENABLED", schedule.last_error)

    def test_daily_run_leaves_other_companies_scheduled(self):
        schedule = RunSchedule.objects.create(
            name="Company B daily",
            enabled=True,
            scope=RunJob.SCOPE_SINGLE,
            company_key="company_b",
            cron_expr="0 18 * * *",
            timezone_name="Africa/Lagos",
            target_date_mode=RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
        )
        with mock.patch.dict("os.environ", {"OIAT_COMPANY_A_DAILY_RUN_ENABLED": "1"}, clear=False), \
                mock.patch("apps.epos_qbo.services.schedule_worker.dispatch_next_queued_job"):
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
