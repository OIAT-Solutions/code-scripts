"""One scheduling authority for the EPOS/QBO workflows (design: docs/SCHEDULING_AUTHORITY.md).

The portal schedule worker (``manage.py run_schedule_worker``) decides *when and what* runs, from
``RunSchedule`` rows; the worker process executes each ``RunJob`` as a subprocess of the existing
business tool. Nothing here posts to QuickBooks or duplicates daily_run's accounting logic.

Ownership of Nora's (company_a) daily routine is a single switch, ``OIAT_COMPANY_A_DAILY_RUN_OWNER``:

* ``ops_scheduler`` (default; the state before the cutover): the akponora-ops cron runs daily_run;
  the portal's "Nora daily routine" schedule is shown but never enqueues (event ``skipped_not_owner``).
* ``portal``: the portal worker enqueues ``company_a_daily`` jobs; the akponora-ops cron refuses to
  run daily_run. Exactly one owner at any time, during and after the migration.

Missed runs (Nora daily routine): a fire that is late by more than ``OIAT_SCHEDULE_MISSED_GRACE_MINUTES``
(180) - the worker was offline - is not run automatically. It is recorded as ``skipped_missed`` and the
business date stays unconfirmed (Home and Slack show it); a person runs it deliberately from the Inbox.
A business date that already has a completed real run is never replayed (``skipped_done``). Company B
keeps its existing behaviour (one catch-up run of the latest closed day on restart).

``expected_confirmed_date`` answers "which business date should be confirmed by now?": the latest
closed date whose scheduled run time (plus a run allowance) has passed. Before tonight's run, the
expected date is the day before, so a day that simply hasn't run yet is not reported missing.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from ..business_date import get_business_day_cutoff, get_business_timezone, get_target_trading_date
from ..models import RunJob, RunSchedule

OWNER_ENV = "OIAT_COMPANY_A_DAILY_RUN_OWNER"
OWNER_OPS, OWNER_PORTAL = "ops_scheduler", "portal"
MISSED_GRACE_ENV = "OIAT_SCHEDULE_MISSED_GRACE_MINUTES"
RUN_ALLOWANCE_ENV = "OIAT_SCHEDULE_RUN_ALLOWANCE_MINUTES"
DAILY_NAME = "Nora daily routine"
COMPANY_A = "company_a"
DAILY_CRON_ENV = "OIAT_COMPANY_A_DAILY_RUN_CRON"
DAILY_ENABLED_ENV = "OIAT_COMPANY_A_DAILY_RUN_ENABLED"
DEFAULT_DAILY_CRON = "0 18 * * *"
COMPLETED_STATUSES = {"ok", "review"}  # daily_run exit 0 / 3: the routine finished


def _env_minutes(name: str, default: int) -> timedelta:
    try:
        return timedelta(minutes=max(1, int(str(os.getenv(name, "")).strip() or default)))
    except ValueError:
        return timedelta(minutes=default)


def company_a_daily_owner() -> str:
    value = str(os.getenv(OWNER_ENV, "")).strip().lower()
    return OWNER_PORTAL if value == OWNER_PORTAL else OWNER_OPS


def portal_owns_company_a_daily() -> bool:
    return company_a_daily_owner() == OWNER_PORTAL


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
    """Create the portal schedule for Nora's daily routine if it is missing (paused: the cutover
    enables it together with ``OIAT_COMPANY_A_DAILY_RUN_OWNER=portal``). Never changes an existing row."""
    existing = daily_routine_schedule()
    if existing is not None:
        return existing, False
    sched = RunSchedule(name=DAILY_NAME, enabled=False, scope=RunJob.SCOPE_COMPANY_A_DAILY, company_key=COMPANY_A,
                        cron_expr=os.getenv(DAILY_CRON_ENV, "").strip() or DEFAULT_DAILY_CRON,
                        timezone_name=os.getenv("SCHEDULE_TZ", "").strip() or "Africa/Lagos",
                        is_system_managed=True, parallel=1, continue_on_failure=False)
    sched.next_fire_at = sched.compute_next_fire_at(from_dt=now or timezone.now())
    sched.save()
    return sched, True


def day_already_ran(day: date) -> bool:
    """A real (not dry) daily_run for ``day`` finished (ok / review): never replay it automatically."""
    from . import company_a_ops as ops

    try:
        runs = ops.list_runs(include_dry=False)
    except Exception:  # noqa: BLE001 - unreadable evidence must not block the schedule silently
        return False
    return any(r.business_date == day.isoformat() and r.status in COMPLETED_STATUSES for r in runs)


# ---------------------------------------------------------------- expected confirmation date
def _cron_and_tz(company_key: str) -> tuple[str, str] | None:
    """The recurring schedule that produces a company's daily sales confirmation."""
    if company_key == COMPANY_A:
        if portal_owns_company_a_daily():
            s = daily_routine_schedule()
            return (s.cron_expr, s.timezone_name) if s is not None and s.enabled and s.cron_expr else None
        if str(os.getenv(DAILY_ENABLED_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}:
            return (os.getenv(DAILY_CRON_ENV, "").strip() or "0 6 * * *",
                    os.getenv("SCHEDULE_TZ", "").strip() or "Africa/Lagos")
        return None
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

    ``owner`` is diagnostic only. ``freshness``: ``current`` (expected date confirmed), ``behind``,
    ``paused`` or ``unknown``. A worker heartbeat is not used as proof of any workflow's health."""
    from . import company_a_ops as ops

    now = now or timezone.now()
    out = []
    # Nora daily routine
    owner = company_a_daily_owner()
    sched = daily_routine_schedule()
    if owner == OWNER_PORTAL:
        enabled = bool(sched and sched.enabled)
        cron = sched.cron_expr if sched else ""
        tz_name = sched.timezone_name if sched else ""
        next_due = sched.next_fire_at if enabled else None
    else:
        info = ops.schedule_info(now)
        enabled, cron, tz_name, next_due = info["enabled"], info["cron"], info["timezone"], info["next_run"]
    try:
        runs = ops.list_runs(include_dry=False)
    except Exception:  # noqa: BLE001
        runs = []
    last = runs[0] if runs else None
    confirmed = sorted({r.business_date for r in runs if r.status in COMPLETED_STATUSES})
    expected = expected_confirmed_date(COMPANY_A, now).isoformat()
    out.append({
        "company_key": COMPANY_A, "workflow": "company_a_daily", "name": DAILY_NAME, "enabled": enabled,
        "completed": False, "cron": cron, "timezone": tz_name, "next_due": next_due,
        "last_run_at": getattr(last, "finished_at", None), "last_business_date": getattr(last, "business_date", None),
        "outcome": getattr(last, "status", None), "expected_date": expected,
        "freshness": ("paused" if not enabled else "current" if expected in confirmed else "behind"),
        "actions": ["run_day"] + (["pause", "resume"] if owner == OWNER_PORTAL else []),
        "owner": owner,
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
            "actions": ["run_now", "pause", "resume"], "owner": OWNER_PORTAL,
        })
    return out
