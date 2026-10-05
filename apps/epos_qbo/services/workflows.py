"""The one EPOS/QBO scheduling authority: the workflow catalogue and its rules.

The portal schedule worker (``manage.py run_schedule_worker``) is the only scheduler and the only process
that starts jobs. A ``RunSchedule`` row says when a catalogue workflow runs for a company; the worker
queues a ``RunJob`` and runs the workflow's existing tool as a subprocess. Nothing here posts to
QuickBooks or duplicates a tool's accounting logic.

Catalogue (``WORKFLOWS``): what can be scheduled, for which companies.
* ``company_a_daily`` Daily routine (Nora / company_a only): code_scripts.akponora_ops.daily_run for one
  closed trading date - products, bills (+ cash payments), sales, item check, stock, banking.
* ``single_company`` Sales sync (any company except company_a, whose sales are in its Daily routine).

Rules for the Daily routine: one schedule per company (duplicates never queue); a fire more than
``OIAT_SCHEDULE_MISSED_GRACE_MINUTES`` (180) late is not run (``skipped_missed``, Slack, Home shows the
day unconfirmed; a person runs it from the Inbox); a day with a completed full routine is never replayed
(``skipped_done``). ``expected_confirmed_date``: the latest closed date whose scheduled run (plus a run
allowance) has passed - a day still to run tonight is not "missing" on Home.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from ..business_date import get_business_day_cutoff, get_business_timezone, get_target_trading_date
from ..models import RunJob, RunSchedule

MISSED_GRACE_ENV = "OIAT_SCHEDULE_MISSED_GRACE_MINUTES"
RUN_ALLOWANCE_ENV = "OIAT_SCHEDULE_RUN_ALLOWANCE_MINUTES"
DAILY_NAME = "Nora daily routine"
COMPANY_A = "company_a"
DEFAULT_DAILY_CRON = "0 18 * * *"  # 18:00 Lagos: staff have the day to correct EPOS first
COMPLETED_STATUSES = {"ok", "review"}  # daily_run exit 0 / 3: the routine finished


def _env_minutes(name: str, default: int) -> timedelta:
    try:
        return timedelta(minutes=max(1, int(str(os.getenv(name, "")).strip() or default)))
    except ValueError:
        return timedelta(minutes=default)


@dataclass(frozen=True)
class Workflow:
    key: str  # the RunJob scope it runs
    name: str
    description: str
    only_companies: tuple = ()  # empty = any company not in except_companies
    except_companies: tuple = ()
    one_per_company: bool = False

    def available_for(self, company_key: str) -> bool:
        if self.only_companies:
            return company_key in self.only_companies
        return company_key not in self.except_companies


WORKFLOWS = {
    RunJob.SCOPE_COMPANY_A_DAILY: Workflow(
        RunJob.SCOPE_COMPANY_A_DAILY, "Daily routine",
        "Products, bills (cash bills paid), sales, item check, stock check and banking from the till sheet, "
        "for the last closed business day.", only_companies=(COMPANY_A,), one_per_company=True),
    RunJob.SCOPE_SINGLE: Workflow(
        RunJob.SCOPE_SINGLE, "Sales sync",
        "Download the day's EPOS sales and post them to QuickBooks.", except_companies=(COMPANY_A,)),
}


def schedulable_workflows(company_key: str | None = None) -> list[Workflow]:
    return [w for w in WORKFLOWS.values() if company_key is None or w.available_for(company_key)]


def missed_grace() -> timedelta:
    return _env_minutes(MISSED_GRACE_ENV, 180)


def run_allowance() -> timedelta:
    return _env_minutes(RUN_ALLOWANCE_ENV, 90)


def is_missed(due_at: datetime | None, now: datetime) -> bool:
    return bool(due_at) and now - due_at > missed_grace()


def business_date_for_fire(due_at: datetime) -> date:
    """The closed trading date a run fired at ``due_at`` is for (bound at enqueue time)."""
    return get_target_trading_date(now=due_at)


# ---------------------------------------------------------------- Nora daily routine schedule
def daily_routine_schedule() -> RunSchedule | None:
    return (RunSchedule.objects.filter(scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key=COMPANY_A)
            .order_by("created_at").first())


def ensure_daily_routine_schedule(*, now: datetime | None = None) -> tuple[RunSchedule, bool]:
    """Create Nora's Daily routine schedule if it is missing (paused). Never changes an existing row."""
    existing = daily_routine_schedule()
    if existing is not None:
        return existing, False
    sched = RunSchedule(name=DAILY_NAME, enabled=False, scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key=COMPANY_A,
                        cron_expr=DEFAULT_DAILY_CRON, timezone_name="Africa/Lagos",
                        parallel=1, continue_on_failure=False)
    sched.next_fire_at = sched.compute_next_fire_at(from_dt=now or timezone.now())
    sched.save()
    return sched, True


def full_routine_completed(run) -> bool:
    """A real run of the WHOLE routine that finished (ok / review). A partial run (``--only`` from the
    Inbox: its unselected steps are 'skipped') does not count, so the full routine still runs."""
    if getattr(run, "dry_run", True) or run.status not in COMPLETED_STATUSES:
        return False
    return not any(getattr(s, "status", "") == "skipped" for s in getattr(run, "steps", []) or [])


def day_already_ran(day: date) -> bool:
    """The full routine already finished for ``day``: never replay it automatically."""
    from . import company_a_ops as ops

    try:
        runs = ops.list_runs(include_dry=False)
    except Exception:  # noqa: BLE001 - unreadable evidence must not block the schedule silently
        return False
    return any(r.business_date == day.isoformat() and full_routine_completed(r) for r in runs)


def sales_confirmed(run) -> bool:
    """Home's rule (experience.verified_sales): sales posted and reconciled MATCH in a real run."""
    s = run.step("sales") if hasattr(run, "step") else None
    return bool(not getattr(run, "dry_run", True) and s and s.status == "ok"
                and s.counts.get("mode") == "post" and s.counts.get("reconcile_status") == "MATCH")


# ---------------------------------------------------------------- expected confirmation date
def _cron_and_tz(company_key: str) -> tuple[str, str] | None:
    """The recurring schedule that produces a company's daily sales confirmation."""
    if company_key == COMPANY_A:
        s = daily_routine_schedule()
        return (s.cron_expr, s.timezone_name) if s is not None and s.enabled and s.cron_expr else None
    s = (RunSchedule.objects.filter(enabled=True, completed_at__isnull=True,
                                    schedule_type=RunSchedule.SCHEDULE_TYPE_RECURRING,
                                    scope__in=[RunJob.SCOPE_SINGLE, RunJob.SCOPE_ALL])
         .filter(company_key=company_key).exclude(cron_expr="").order_by("created_at").first()
         or RunSchedule.objects.filter(enabled=True, completed_at__isnull=True, scope=RunJob.SCOPE_ALL,
                                       schedule_type=RunSchedule.SCHEDULE_TYPE_RECURRING)
         .exclude(cron_expr="").order_by("created_at").first())
    return (s.cron_expr, s.timezone_name) if s is not None else None


def scheduled_run_for(day: date, company_key: str) -> datetime | None:
    """When the scheduled run for business ``day`` fires: the first fire after that day closes."""
    found = _cron_and_tz(company_key)
    if not found:
        return None
    cron, tz_name = found
    try:
        from croniter import croniter

        tz = ZoneInfo(tz_name)
        hour, minute = get_business_day_cutoff()
        closes = datetime.combine(day + timedelta(days=1), time(hour, minute), tzinfo=get_business_timezone())
        return croniter(cron, closes.astimezone(tz)).get_next(datetime)
    except Exception:  # noqa: BLE001 - an invalid cron shows up elsewhere; fall back to the closed date
        return None


def expected_confirmed_date(company_key: str, now: datetime | None = None) -> date:
    """Latest business date that should be confirmed by now (see the module doc)."""
    now = now or timezone.now()
    closed = get_target_trading_date(now=now)
    fire = scheduled_run_for(closed, company_key)
    if fire is not None and now < fire + run_allowance():
        return closed - timedelta(days=1)
    return closed


# ---------------------------------------------------------------- summary for the interface
def workflow_summaries(now: datetime | None = None) -> list[dict]:
    """Company-neutral schedule summary per workflow (for Home / Schedules / the agency console).

    ``freshness``: ``current`` (expected date confirmed), ``behind``,
    ``paused`` or ``unknown``. A worker heartbeat is not used as proof of any workflow's health."""
    from . import company_a_ops as ops

    now = now or timezone.now()
    out = []
    # Daily routine
    sched = daily_routine_schedule()
    enabled = bool(sched and sched.enabled)
    cron = sched.cron_expr if sched else ""
    tz_name = sched.timezone_name if sched else ""
    next_due = sched.next_fire_at if enabled else None
    try:
        runs = ops.list_runs(include_dry=False)
    except Exception:  # noqa: BLE001
        runs = []
    last = runs[0] if runs else None
    confirmed = sorted({r.business_date for r in runs if sales_confirmed(r)})
    expected = expected_confirmed_date(COMPANY_A, now).isoformat()
    out.append({
        "company_key": COMPANY_A, "workflow": RunJob.SCOPE_COMPANY_A_DAILY, "name": WORKFLOWS[RunJob.SCOPE_COMPANY_A_DAILY].name, "enabled": enabled,
        "completed": False, "cron": cron, "timezone": tz_name, "next_due": next_due,
        "last_run_at": getattr(last, "finished_at", None), "last_business_date": getattr(last, "business_date", None),
        "outcome": getattr(last, "status", None), "expected_date": expected,
        "freshness": ("paused" if not enabled else "current" if expected in confirmed else "behind"),
        "actions": ["run_day", "pause", "resume"],
    })
    # every other portal schedule (Goldplates sales, inventory ...)
    for s in RunSchedule.objects.exclude(scope=RunJob.SCOPE_COMPANY_A_DAILY).order_by("name"):
        job = s.scheduled_jobs.order_by("-created_at").first()
        out.append({
            "company_key": s.company_key or "", "workflow": s.scope, "name": s.name, "enabled": s.enabled,
            "completed": s.completed_at is not None, "cron": s.cron_expr, "timezone": s.timezone_name,
            "next_due": s.next_fire_at if s.enabled else None, "last_run_at": getattr(job, "finished_at", None),
            "last_business_date": getattr(getattr(job, "target_date", None), "isoformat", lambda: None)(),
            "outcome": getattr(job, "status", None) or s.last_result or None, "expected_date": None,
            "freshness": "paused" if not s.enabled else "unknown",
            "actions": ["run_now", "pause", "resume"],
        })
    return out
