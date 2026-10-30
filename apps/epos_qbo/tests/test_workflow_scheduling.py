"""One scheduling authority (services/workflows.py): the catalogue, missed runs, business dates, dispatch.

Synthetic state only: no subprocess is started, no Slack is sent, nothing reaches QuickBooks."""
from __future__ import annotations

import os
from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.epos_qbo.models import CompanyConfigRecord, RunJob, RunLock, RunSchedule, RunScheduleEvent
from apps.epos_qbo.services import job_runner, schedule_worker, workflows
from apps.epos_qbo.tests.test_company_a_ops import CompanyAOpsFixtureMixin, _summary

LAGOS = ZoneInfo("Africa/Lagos")


def lagos(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=LAGOS)


class Base(CompanyAOpsFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch("apps.epos_qbo.services.schedule_worker._notify_missed")
        self.notify = p.start()
        self.addCleanup(p.stop)

    def env(self, values):
        p = mock.patch.dict(os.environ, values, clear=False)
        p.start()
        self.addCleanup(p.stop)

    def nora_schedule(self, *, enabled=True, next_fire=None):
        s, _ = workflows.ensure_daily_routine_schedule(now=lagos(2026, 10, 5, 8))
        s.enabled = enabled
        s.next_fire_at = next_fire or lagos(2026, 10, 5, 18)
        s.save()
        return s

    def events(self):
        return list(RunScheduleEvent.objects.filter(schedule__scope=RunJob.SCOPE_COMPANY_A_DAILY)
                    .order_by("created_at").values_list("event_type", flat=True))


class DailyRoutineScheduleTests(Base):
    def test_ensure_creates_one_paused_schedule(self):
        s, created = workflows.ensure_daily_routine_schedule(now=lagos(2026, 10, 5, 8))
        self.assertTrue(created)
        self.assertFalse(s.enabled)
        self.assertEqual((s.scope, s.company_key, s.cron_expr, s.timezone_name),
                         (RunJob.SCOPE_COMPANY_A_DAILY, "company_a", "0 18 * * *", "Africa/Lagos"))
        self.assertEqual(s.next_fire_at, lagos(2026, 10, 5, 18))
        self.assertEqual(workflows.ensure_daily_routine_schedule()[1], False)
        self.assertEqual(RunSchedule.objects.filter(scope=RunJob.SCOPE_COMPANY_A_DAILY).count(), 1)

    def test_the_schedule_queues_the_routine_for_the_closed_day(self):
        s = self.nora_schedule()
        with mock.patch.object(schedule_worker, "dispatch_next_queued_job") as dispatch:
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 18, 0, ))
        job = RunJob.objects.get()
        self.assertEqual((job.scope, job.company_key, job.target_date), (RunJob.SCOPE_COMPANY_A_DAILY, "company_a",
                                                                         date(2026, 10, 4)))
        self.assertEqual(job.scheduled_by, s)
        dispatch.assert_called()  # the worker starts it in the same cycle
        s.refresh_from_db()
        self.assertEqual(s.next_fire_at, lagos(2026, 10, 6, 18))

    def test_a_second_nora_schedule_never_queues(self):
        self.nora_schedule()
        dup = RunSchedule.objects.create(name="copy", scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key="company_a",
                                         cron_expr="0 18 * * *", timezone_name="Africa/Lagos", enabled=True,
                                         next_fire_at=lagos(2026, 10, 5, 18))
        with mock.patch.object(schedule_worker, "dispatch_next_queued_job"):
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 18, 1))
        self.assertEqual(RunJob.objects.count(), 1)
        self.assertNotEqual(RunJob.objects.get().scheduled_by_id, dup.pk)
        self.assertTrue(RunScheduleEvent.objects.filter(schedule=dup, event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID).exists())

    def test_paused_schedule_queues_nothing(self):
        self.nora_schedule(enabled=False)
        schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 18, 1))
        self.assertEqual(RunJob.objects.count(), 0)


class MissedAndDuplicateTests(Base):
    def test_late_fire_is_missed_not_run_and_never_replayed(self):
        s = self.nora_schedule()
        with self.captureOnCommitCallbacks(execute=True):  # the alert is sent after the commit
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 22))  # worker was down 18:00-22:00
        self.assertEqual(RunJob.objects.count(), 0)
        self.assertEqual(self.events(), [RunScheduleEvent.TYPE_SKIPPED_MISSED])
        ev = RunScheduleEvent.objects.get(event_type=RunScheduleEvent.TYPE_SKIPPED_MISSED)
        self.assertEqual(ev.payload_json["target_date"], "2026-10-04")
        self.notify.assert_called_once()
        s.refresh_from_db()
        self.assertEqual(s.next_fire_at, lagos(2026, 10, 6, 18))
        schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 23))  # restart again: nothing replays
        self.assertEqual(RunJob.objects.count(), 0)

    def test_a_little_late_still_runs_for_the_scheduled_day(self):
        self.nora_schedule()
        with mock.patch.object(schedule_worker, "dispatch_next_queued_job"):
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 20))  # 2h late: within the 3h grace
        self.assertEqual(RunJob.objects.get().target_date, date(2026, 10, 4))

    def test_completed_day_is_not_replayed(self):
        self.make_run("2026-10-04", "run_170000Z", _summary("2026-10-04"))  # e.g. run by hand from the Inbox
        self.nora_schedule()
        schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 18, 1))
        self.assertEqual(RunJob.objects.count(), 0)
        self.assertEqual(self.events(), [RunScheduleEvent.TYPE_SKIPPED_DONE])

    def test_preview_or_failed_run_does_not_count_as_done(self):
        self.make_run("2026-10-04", "run_160000Z_dry", _summary("2026-10-04", dry=True))
        self.make_run("2026-10-04", "run_150000Z", _summary("2026-10-04", status="failed", exit_code=2))
        self.assertFalse(workflows.day_already_ran(date(2026, 10, 4)))

    def test_duplicate_cycles_queue_once_and_overlap_is_skipped(self):
        s = self.nora_schedule()
        with mock.patch.object(schedule_worker, "dispatch_next_queued_job"):
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 18, 0))
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 18, 0, ))  # same instant again
        self.assertEqual(RunJob.objects.count(), 1)
        RunJob.objects.update(status=RunJob.STATUS_RUNNING, started_at=lagos(2026, 10, 5, 18, 0))
        s.refresh_from_db()
        s.next_fire_at = lagos(2026, 10, 5, 18, 5)
        s.save()
        with mock.patch.object(schedule_worker, "_reconcile_stale_runs", return_value=0), \
                mock.patch.object(schedule_worker, "dispatch_next_queued_job"):
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 18, 6))
        self.assertEqual(RunJob.objects.count(), 1)
        self.assertIn(RunScheduleEvent.TYPE_SKIPPED_OVERLAP, self.events())


class ExecutionTests(Base):
    def test_command_is_the_unchanged_daily_run_for_the_bound_date(self):
        job = RunJob.objects.create(scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key="company_a",
                                    target_date=date(2026, 10, 4))
        cmd = job_runner.build_command_for_job(job)
        self.assertEqual(cmd[1:], ["-m", "code_scripts.akponora_ops.daily_run", "--date", "2026-10-04"])
        self.assertTrue(job_runner.succeeded(job, 0))
        self.assertTrue(job_runner.succeeded(job, 3))  # finished; items wait for review in its evidence
        self.assertFalse(job_runner.succeeded(job, 2))
        other = RunJob(scope=RunJob.SCOPE_SINGLE)
        self.assertFalse(job_runner.succeeded(other, 3))  # other workflows unchanged
        with self.assertRaises(ValueError):
            job_runner.build_command_for_job(RunJob(scope=RunJob.SCOPE_COMPANY_A_DAILY))

    def test_pages_never_start_jobs(self):
        """Only the schedule worker starts jobs: no view module calls the dispatcher."""
        from pathlib import Path

        app = Path(job_runner.__file__).resolve().parents[1]
        for path in sorted(app.glob("views*.py")):
            self.assertNotIn("dispatch_next_queued_job", path.read_text(encoding="utf-8"), path.name)

    def test_worker_dispatch_wraps_the_job_in_the_portal_lock(self):
        RunJob.objects.create(scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key="company_a",
                              target_date=date(2026, 10, 4), status=RunJob.STATUS_QUEUED)
        with mock.patch.object(job_runner, "start_run_job", side_effect=lambda job, cmd: job):
            job, result = job_runner.dispatch_next_queued_job()
        self.assertEqual(result, "started")
        self.assertTrue(RunLock.objects.get(pk=1).active)  # the portal lock wraps the job ...

    def test_dispatch_never_takes_the_global_file_lock(self):
        """... and daily_run takes the global file lock itself: no nested acquisition, no deadlock."""
        from code_scripts import run_lock

        RunJob.objects.create(scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key="company_a",
                              target_date=date(2026, 10, 4), status=RunJob.STATUS_QUEUED)
        with mock.patch.object(job_runner, "start_run_job", side_effect=lambda job, cmd: job), \
                mock.patch.object(run_lock, "acquire_global_lock", side_effect=AssertionError("no flock here"),
                                  create=True), \
                mock.patch.object(run_lock, "hold_global_lock", side_effect=AssertionError("no flock here")):
            job_runner.dispatch_next_queued_job()


class ExpectedDateTests(Base):
    def setUp(self):
        super().setUp()
        CompanyConfigRecord.objects.create(company_key="company_a", display_name="Akponora", is_active=True)
        CompanyConfigRecord.objects.create(company_key="company_b", display_name="Goldplates", is_active=True)
        RunSchedule.objects.create(name="Goldplates daily", scope=RunJob.SCOPE_SINGLE, company_key="company_b",
                                   cron_expr="0 19 * * *", timezone_name="Africa/Lagos", enabled=True)

    def test_a_day_whose_run_is_still_to_come_is_not_expected(self):
        self.nora_schedule()
        self.assertEqual(workflows.expected_confirmed_date("company_a", lagos(2026, 10, 5, 8)), date(2026, 10, 3))
        self.assertEqual(workflows.expected_confirmed_date("company_a", lagos(2026, 10, 5, 19, 29)), date(2026, 10, 3))
        self.assertEqual(workflows.expected_confirmed_date("company_a", lagos(2026, 10, 5, 19, 31)), date(2026, 10, 4))
        self.assertEqual(workflows.expected_confirmed_date("company_b", lagos(2026, 10, 5, 20)), date(2026, 10, 3))
        self.assertEqual(workflows.expected_confirmed_date("company_b", lagos(2026, 10, 5, 21)), date(2026, 10, 4))
        # before the 05:00 cutoff the closed day is two days back; its run is long done
        self.assertEqual(workflows.expected_confirmed_date("company_a", lagos(2026, 10, 5, 4)), date(2026, 10, 3))

    def test_home_does_not_flag_tonights_day_as_missing(self):
        """Marvin, 5 Oct: 1-3 Oct posted, 4 Oct runs at 18:00 -> no 'missing day' banner at 08:00."""
        from apps.epos_qbo.services import experience

        self.nora_schedule()
        steps = [{"name": "sales", "status": "ok", "counts": {"mode": "post", "reconcile_status": "MATCH",
                                                                "qbo_total": 1000}}]
        for d in ("2026-10-01", "2026-10-02", "2026-10-03"):
            self.make_run(d, "run_170000Z", _summary(d, steps=steps))
        with mock.patch.object(experience, "inbox", return_value=([], [])):
            ctx = experience.home_context(company_key="company_a", now=lagos(2026, 10, 5, 8))
        row = ctx["home_rows"][0]
        self.assertEqual(row["missing"], [])
        self.assertEqual(ctx["home_expected"], date(2026, 10, 3))
        with mock.patch.object(experience, "inbox", return_value=([], [])):
            ctx = experience.home_context(company_key="company_a", now=lagos(2026, 10, 5, 21))
        self.assertEqual(ctx["home_rows"][0]["missing"], [date(2026, 10, 4)])  # after the run time it is missing

    def test_trading_date_contract_matches_daily_run(self):
        from code_scripts.akponora_ops.daily_run import last_closed_business_date
        from apps.epos_qbo.business_date import get_target_trading_date

        for t in (lagos(2026, 10, 5, 4, 59), lagos(2026, 10, 5, 5, 0), lagos(2026, 10, 5, 18), lagos(2026, 10, 5, 23, 59)):
            self.assertEqual(get_target_trading_date(now=t), last_closed_business_date(t), t)

    def test_summary_has_the_interface_fields(self):
        self.nora_schedule()
        rows = workflows.workflow_summaries(now=lagos(2026, 10, 5, 8))
        nora = rows[0]
        for key in ("company_key", "workflow", "name", "enabled", "completed", "cron", "timezone", "next_due",
                    "last_run_at", "last_business_date", "outcome", "freshness", "actions"):
            self.assertIn(key, nora)
        self.assertEqual((nora["workflow"], nora["name"], nora["enabled"]), ("company_a_daily", "Daily routine", True))
        self.assertEqual(nora["expected_date"], "2026-10-03")
        self.assertIn("Goldplates daily", [r["name"] for r in rows])



class ReviewFixTests(Base):
    """Findings of the independent review (5 Oct 2026)."""

    def test_a_worker_cycle_never_switches_the_nora_schedule_off(self):
        s = self.nora_schedule()
        RunSchedule.objects.create(name="Goldplates daily", scope=RunJob.SCOPE_SINGLE, company_key="company_b",
                                   cron_expr="0 19 * * *", timezone_name="Africa/Lagos", enabled=True)
        with mock.patch.object(schedule_worker, "dispatch_next_queued_job"):
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 12))
        s.refresh_from_db()
        self.assertTrue(s.enabled)

    def test_held_flock_keeps_a_silent_daily_job_alive_across_containers(self):
        from apps.epos_qbo.services import run_reconciler as rr

        job = RunJob.objects.create(scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key="company_a",
                                    target_date=date(2026, 10, 4), status=RunJob.STATUS_RUNNING,
                                    started_at=lagos(2026, 10, 5, 18), pid=999999)  # PID not visible here
        verdict = rr.assess_running_job(job, now=lagos(2026, 10, 5, 18, 40), lock_free=False)
        self.assertFalse(verdict.stale)
        verdict = rr.assess_running_job(job, now=lagos(2026, 10, 5, 18, 40), lock_free=True)
        self.assertTrue(verdict.stale)  # lock free after the grace: really gone

    def test_partial_inbox_run_does_not_count_as_the_day_done(self):
        steps = [{"name": "catalogue", "status": "skipped", "detail": "not selected (--only)"},
                 {"name": "bills", "status": "ok"}]
        self.make_run("2026-10-04", "run_110000Z", _summary("2026-10-04", steps=steps))
        self.assertFalse(workflows.day_already_ran(date(2026, 10, 4)))
        self.make_run("2026-10-04", "run_170000Z", _summary("2026-10-04", steps=[{"name": "bills", "status": "ok"}]))
        self.assertTrue(workflows.day_already_ran(date(2026, 10, 4)))

    def test_multi_day_outage_lists_every_missed_day_once(self):
        self.nora_schedule(next_fire=lagos(2026, 10, 3, 18))
        self.make_run("2026-10-03", "run_100000Z", _summary("2026-10-03"))  # run by hand meanwhile
        schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 22))
        ev = RunScheduleEvent.objects.get(event_type=RunScheduleEvent.TYPE_SKIPPED_MISSED)
        self.assertEqual(ev.payload_json["target_dates"], ["2026-10-02", "2026-10-04"])  # 3 Oct already ran
        self.assertEqual(RunJob.objects.count(), 0)

    def test_dispatch_cancels_a_job_whose_day_finished_meanwhile(self):
        job = RunJob.objects.create(scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key="company_a",
                                    target_date=date(2026, 10, 4), status=RunJob.STATUS_QUEUED)
        self.make_run("2026-10-04", "run_170000Z", _summary("2026-10-04"))
        with mock.patch.object(job_runner, "start_run_job") as start:
            self.assertEqual(job_runner.dispatch_next_queued_job(), (None, "empty"))
            start.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, RunJob.STATUS_CANCELLED)
        self.assertFalse(RunLock.objects.get(pk=1).active)

    def test_nora_schedule_needs_company_a(self):
        odd = RunSchedule.objects.create(name="odd", scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key="company_b",
                                         cron_expr="0 18 * * *", timezone_name="Africa/Lagos", enabled=True,
                                         next_fire_at=lagos(2026, 10, 5, 18))
        schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 18, 1))
        self.assertEqual(RunJob.objects.count(), 0)
        self.assertTrue(RunScheduleEvent.objects.filter(schedule=odd, event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID).exists())



class SchedulesPageTests(Base):
    """Marvin, 5 Oct: one row per workflow - no env-cron row next to the portal schedule."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User

        CompanyConfigRecord.objects.create(company_key="company_a", display_name="AKPONORA VENTURES LTD.", is_active=True)
        self.client.force_login(User.objects.create_superuser("admin", "", "pw"))
        self.make_run("2026-10-04", "run_110000Z_dry", _summary("2026-10-04", dry=True))  # a dry run in progress

    def rows(self, **params):
        from django.urls import reverse

        return self.client.get(reverse("epos_qbo:schedules"), params).context["schedule_display_rows"]

    def test_one_daily_routine_row(self):
        self.nora_schedule(enabled=True)
        routine = [r for r in self.rows() if r["name"] == "Daily routine"]
        self.assertEqual(len(routine), 1)
        self.assertEqual(routine[0]["company"], "AKPONORA VENTURES LTD.")
        self.assertEqual(routine[0]["outcome"], "Not recorded")  # the dry run is not the last attempt
        self.assertFalse([r for r in self.rows() if r["name"] == workflows.DAILY_NAME])

    def test_paused_daily_routine_is_one_row_too(self):
        self.nora_schedule(enabled=False)
        routine = [r for r in self.rows(state="paused") if r["name"] == "Daily routine"]
        self.assertEqual(len(routine), 1)
        self.assertEqual(routine[0]["state"], "paused")


class CatalogueTests(Base):
    """'Add schedule' offers the workflow catalogue: Daily routine (Nora only), Sales sync (others)."""

    def setUp(self):
        super().setUp()
        CompanyConfigRecord.objects.create(company_key="company_a", display_name="Akponora", is_active=True)
        CompanyConfigRecord.objects.create(company_key="company_b", display_name="Goldplates", is_active=True)

    def form(self, **data):
        from apps.epos_qbo.forms import RunScheduleForm

        base = {"name": "s", "enabled": "on", "schedule_type": RunSchedule.SCHEDULE_TYPE_RECURRING,
                "cron_expr": "0 18 * * *", "timezone_name": "Africa/Lagos", "parallel": "1",
                "stagger_seconds": "0", "target_date_mode": "trading_date"}
        base.update(data)
        return RunScheduleForm(data=base)

    def test_catalogue_per_company(self):
        self.assertEqual([w.name for w in workflows.schedulable_workflows("company_a")], ["Daily routine"])
        self.assertEqual([w.name for w in workflows.schedulable_workflows("company_b")], ["Sales sync"])

    def test_daily_routine_only_for_company_a_and_only_once(self):
        from apps.epos_qbo.forms import RunScheduleForm

        WORKFLOW_DAILY_ROUTINE = RunScheduleForm.WORKFLOW_DAILY_ROUTINE
        f = self.form(workflow=WORKFLOW_DAILY_ROUTINE, company_target="one", company_key="company_b")
        self.assertFalse(f.is_valid())
        f = self.form(workflow=WORKFLOW_DAILY_ROUTINE, company_target="one", company_key="company_a")
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data["scope"], RunJob.SCOPE_COMPANY_A_DAILY)
        self.nora_schedule()
        f = self.form(workflow=WORKFLOW_DAILY_ROUTINE, company_target="one", company_key="company_a")
        self.assertFalse(f.is_valid())  # one Daily routine per company

    def test_sales_sync_refuses_company_a_and_inventory_is_gone(self):
        f = self.form(workflow="sales", company_target="one", company_key="company_a")
        self.assertFalse(f.is_valid())
        f = self.form(workflow="sales", company_target="one", company_key="company_b")
        self.assertTrue(f.is_valid(), f.errors)
        f = self.form(workflow="inventory", company_target="one", company_key="company_b")
        self.assertFalse(f.is_valid())

    def test_all_companies_sales_never_includes_company_a(self):
        RunSchedule.objects.create(name="All", scope=RunJob.SCOPE_ALL, cron_expr="0 19 * * *",
                                   timezone_name="Africa/Lagos", enabled=True, next_fire_at=lagos(2026, 10, 5, 19))
        with mock.patch.object(schedule_worker, "dispatch_next_queued_job"):
            schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 19, 1))
        job = RunJob.objects.get()
        self.assertIn("company_a", (job.inventory_options_json or {}).get("exclude_companies", []))

    def test_single_company_a_sales_schedule_never_queues(self):
        s = RunSchedule.objects.create(name="Nora sales", scope=RunJob.SCOPE_SINGLE, company_key="company_a",
                                       cron_expr="0 19 * * *", timezone_name="Africa/Lagos", enabled=True,
                                       next_fire_at=lagos(2026, 10, 5, 19))
        schedule_worker.process_schedule_cycle(now=lagos(2026, 10, 5, 19, 1))
        self.assertEqual(RunJob.objects.count(), 0)
        self.assertTrue(RunScheduleEvent.objects.filter(schedule=s).exists())
