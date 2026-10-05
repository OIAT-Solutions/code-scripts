from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.epos_qbo.business_date import get_target_trading_date

from ..models import RunJob, RunLock, RunSchedule, RunScheduleEvent, SchedulerWorkerHeartbeat
from .job_runner import dispatch_next_queued_job
from .run_reconciler import describe_run, describe_run_short, reconcile_stale_running_jobs

logger = logging.getLogger(__name__)

DEFAULT_WORKER_POLL_SECONDS = 15
DEFAULT_FALLBACK_CRON = "0 18 * * *"  # 6pm daily
FALLBACK_SCHEDULE_NAME = "Legacy Env Fallback"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    return value


def configured_poll_seconds() -> int:
    return _env_int(
        "OIAT_SCHEDULER_POLL_SECONDS",
        DEFAULT_WORKER_POLL_SECONDS,
        minimum=1,
    )


def env_fallback_enabled() -> bool:
    return _env_flag("OIAT_SCHEDULER_ENABLE_ENV_FALLBACK", True)


COMPANY_A_KEY = "company_a"


def company_a_sales_automation_enabled() -> bool:
    """Whether scheduled runs may include Company A sales (default: no, until W9)."""
    return _env_flag("OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED", False)


def company_a_daily_run_enabled() -> bool:
    """When on, Company A runs only through ``code_scripts.akponora_ops.daily_run`` (scheduled by the
    akponora-ops container), so this worker never schedules Company A sales itself."""
    return _env_flag("OIAT_COMPANY_A_DAILY_RUN_ENABLED", False)


def company_a_scheduled_sales_allowed() -> bool:
    return company_a_sales_automation_enabled() and not company_a_daily_run_enabled()


def company_a_blocked_message() -> str:
    if company_a_daily_run_enabled():
        return ("Company A is scheduled by the Akponora daily run (OIAT_COMPANY_A_DAILY_RUN_ENABLED=1); "
                "this schedule does not run Company A to avoid a double run.")
    return ("Company A sales automation is disabled until go-live; "
            "set OIAT_COMPANY_A_SALES_AUTOMATION_ENABLED=1 to allow scheduled Company A sales runs.")


def _default_schedule_timezone() -> str:
    return str(
        getattr(
            settings,
            "OIAT_BUSINESS_TIMEZONE",
            getattr(settings, "TIME_ZONE", "UTC"),
        )
    )


def _fallback_cron_expr() -> str:
    value = (os.getenv("SCHEDULE_CRON") or DEFAULT_FALLBACK_CRON).strip()
    return value or DEFAULT_FALLBACK_CRON


def _fallback_timezone_name() -> str:
    value = (os.getenv("SCHEDULE_TZ") or "").strip()
    if value:
        return value
    return _default_schedule_timezone()


def _create_event(
    *,
    schedule: RunSchedule | None,
    event_type: str,
    message: str,
    run_job: RunJob | None = None,
    payload: dict[str, Any] | None = None,
) -> RunScheduleEvent:
    payload_json = dict(payload or {})
    if schedule is not None:
        payload_json.setdefault("schedule_id", str(schedule.id))
        payload_json.setdefault("schedule_name", schedule.name)
        payload_json.setdefault("schedule_scope", schedule.scope)
        payload_json.setdefault("schedule_type", schedule.schedule_type)
    return RunScheduleEvent.objects.create(
        schedule=schedule,
        run_job=run_job,
        event_type=event_type,
        message=message,
        payload_json=payload_json,
    )


def _active_scheduled_run(schedule: RunSchedule) -> RunJob | None:
    return (
        RunJob.objects.filter(
            scheduled_by=schedule,
            status__in=[RunJob.STATUS_QUEUED, RunJob.STATUS_RUNNING],
        )
        .order_by("created_at")
        .first()
    )


def _active_scheduled_run_exists(schedule: RunSchedule) -> bool:
    return _active_scheduled_run(schedule) is not None


def _active_global_run() -> tuple[bool, RunJob | None, str]:
    """(active, blocking job if known, lock holder) for the global run check."""
    lock = RunLock.objects.filter(active=True).select_related("owner_run_job").first()
    if lock is not None:
        return True, lock.owner_run_job, lock.holder or ""
    job = RunJob.objects.filter(status=RunJob.STATUS_RUNNING).order_by("created_at").first()
    return job is not None, job, ""


def _active_global_run_exists() -> bool:
    return _active_global_run()[0]


def _skip_overlap(
    schedule: RunSchedule,
    *,
    current: datetime,
    blocking_job: RunJob | None,
    holder: str = "",
) -> tuple[None, str]:
    """Record an overlap skip that names the blocking run and how long it has run."""
    payload: dict[str, Any] = {}
    if blocking_job is not None:
        blocking = describe_run(blocking_job, now=current)
        message = f"Skipped because another run is active: {blocking}."
        last_error = describe_run_short(blocking_job, now=current)
        payload = {
            "blocking_run_id": str(blocking_job.id),
            "blocking_run_status": blocking_job.status,
            "blocking_run_company": blocking_job.company_key,
        }
    elif holder:
        message = f"Skipped because another run is active: run lock held by {holder}."
        last_error = f"Blocked by run lock holder {holder}"
        payload = {"blocking_lock_holder": holder}
    else:
        message = "Skipped because another run is active."
        last_error = ""
    schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_OVERLAP
    schedule.last_error = last_error
    schedule.last_fired_at = current
    schedule.save(update_fields=["last_result", "last_error", "last_fired_at", "updated_at"])
    # The blocking run is named in the message/payload, not linked as run_job: the
    # schedules page reads an event's run_job as "this schedule's run".
    _create_event(
        schedule=schedule,
        event_type=RunScheduleEvent.TYPE_SKIPPED_OVERLAP,
        message=message,
        payload=payload,
    )
    return None, RunScheduleEvent.TYPE_SKIPPED_OVERLAP


def _reconcile_stale_runs(now: datetime) -> int:
    """Close runs stuck in 'running' so they cannot block schedules forever."""
    try:
        closed = reconcile_stale_running_jobs(now=now)
    except Exception:
        logger.exception("Stale run reconciliation failed")
        return 0
    return len(closed)


def _schedule_requires_company(schedule: RunSchedule) -> bool:
    return schedule.scope in {RunJob.SCOPE_SINGLE, RunJob.SCOPE_INVENTORY_PIPELINE}


def _is_blocked_company_a_sales_schedule(schedule: RunSchedule) -> bool:
    return (
        schedule.scope == RunJob.SCOPE_SINGLE
        and (schedule.company_key or "").strip() == COMPANY_A_KEY
        and not company_a_scheduled_sales_allowed()
    )


def _job_payload_from_schedule(schedule: RunSchedule, *, now: datetime, target_date=None) -> dict[str, Any]:
    if schedule.scope == RunJob.SCOPE_COMPANY_A_DAILY:
        # One closed trading date, bound when queued (workflows.business_date_for_fire); never "today".
        return {"scope": schedule.scope, "company_key": schedule.company_key or "company_a",
                "target_date": target_date or get_target_trading_date(now=now), "parallel": 1,
                "stagger_seconds": 0, "continue_on_failure": False, "inventory_options_json": {},
                "status": RunJob.STATUS_QUEUED, "scheduled_by": schedule,
                "command_display": f"schedule:{schedule.name}"}
    target_date = None
    inventory_options: dict[str, Any] = {}
    if schedule.scope in {RunJob.SCOPE_SINGLE, RunJob.SCOPE_INVENTORY_PIPELINE}:
        parallel = 1
        continue_on_failure = False
    else:
        parallel = max(1, int(schedule.parallel))
        continue_on_failure = bool(schedule.continue_on_failure)
    if schedule.scope == RunJob.SCOPE_INVENTORY_PIPELINE:
        inventory_options = (
            dict(schedule.inventory_options_json)
            if isinstance(schedule.inventory_options_json, dict)
            else {}
        )
    else:
        target_date = get_target_trading_date(now=now)
    # Company A sales stay out of automated all-company runs (system fallback and
    # user-created schedules alike) until W9 go-live flips the opt-in flag, and always
    # while the Akponora daily run owns Company A (no double runs).
    if schedule.scope == RunJob.SCOPE_ALL and not company_a_scheduled_sales_allowed():
        inventory_options["exclude_companies"] = [COMPANY_A_KEY]
    return {
        "scope": schedule.scope,
        "company_key": schedule.company_key or None,
        "target_date": target_date,
        "parallel": parallel,
        "stagger_seconds": max(0, int(schedule.stagger_seconds)),
        "continue_on_failure": continue_on_failure,
        "inventory_options_json": inventory_options,
        "status": RunJob.STATUS_QUEUED,
        "scheduled_by": schedule,
        "command_display": f"schedule:{schedule.name}",
    }


def _initialize_missing_next_fire(schedule: RunSchedule, *, now: datetime) -> bool:
    if not schedule.enabled or schedule.next_fire_at is not None:
        return False
    try:
        schedule.next_fire_at = schedule.compute_next_fire_at(from_dt=now)
        schedule.last_error = ""
        schedule.save(update_fields=["next_fire_at", "last_error", "updated_at"])
    except Exception as exc:
        schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
        schedule.last_error = str(exc)
        schedule.save(update_fields=["last_result", "last_error", "updated_at"])
        _create_event(
            schedule=schedule,
            event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
            message=f"Schedule is invalid and cannot be initialized: {exc}",
        )
    return True


def _upsert_env_fallback_schedule(*, now: datetime) -> dict[str, int]:
    stats = {"fallback_enabled": 0, "fallback_disabled": 0}

    if not env_fallback_enabled():
        for schedule in RunSchedule.objects.filter(is_system_managed=True, enabled=True):
            schedule.enabled = False
            schedule.save(update_fields=["enabled", "updated_at"])
            _create_event(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_FALLBACK_DISABLED,
                message="Schedule is disabled.",
            )
            stats["fallback_disabled"] += 1
        return stats

    has_enabled_user_sales_schedule = RunSchedule.objects.filter(
        enabled=True,
        is_system_managed=False,
        scope__in=[RunJob.SCOPE_ALL, RunJob.SCOPE_SINGLE],
    ).exists()

    if has_enabled_user_sales_schedule:
        for schedule in RunSchedule.objects.filter(is_system_managed=True, enabled=True):
            schedule.enabled = False
            schedule.save(update_fields=["enabled", "updated_at"])
            _create_event(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_FALLBACK_DISABLED,
                message="Schedule is disabled because a sales schedule is configured.",
            )
            stats["fallback_disabled"] += 1
        return stats

    schedule, created = RunSchedule.objects.get_or_create(
        name=FALLBACK_SCHEDULE_NAME,
        is_system_managed=True,
        defaults={
            "enabled": True,
            "scope": RunJob.SCOPE_ALL,
            "company_key": None,
            "cron_expr": _fallback_cron_expr(),
            "timezone_name": _fallback_timezone_name(),
            "target_date_mode": RunSchedule.TARGET_DATE_MODE_TRADING_DATE,
            "parallel": 2,
            "stagger_seconds": 2,
            "continue_on_failure": False,
        },
    )

    changed = created
    cron_expr = _fallback_cron_expr()
    timezone_name = _fallback_timezone_name()
    if schedule.cron_expr != cron_expr:
        schedule.cron_expr = cron_expr
        changed = True
    if schedule.timezone_name != timezone_name:
        schedule.timezone_name = timezone_name
        changed = True
    if not schedule.enabled:
        schedule.enabled = True
        changed = True

    if schedule.next_fire_at is None:
        changed = True
    if changed:
        try:
            schedule.next_fire_at = schedule.compute_next_fire_at(from_dt=now)
            schedule.last_error = ""
        except Exception as exc:
            schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
            schedule.last_error = str(exc)
        update_fields = [
            "enabled",
            "cron_expr",
            "timezone_name",
            "next_fire_at",
            "last_result",
            "last_error",
            "updated_at",
        ]
        schedule.save(update_fields=update_fields)
        _create_event(
            schedule=schedule,
            event_type=RunScheduleEvent.TYPE_FALLBACK_ENABLED,
            message="Schedule enabled from environment configuration.",
            payload={"cron_expr": schedule.cron_expr, "timezone_name": schedule.timezone_name},
        )
        stats["fallback_enabled"] += 1

    return stats


def enqueue_run_for_schedule(
    schedule: RunSchedule,
    *,
    now: datetime | None = None,
    source: str = "manual",
    target_date=None,
) -> tuple[RunJob | None, str]:
    current = now or timezone.now()
    if schedule.scope == RunJob.SCOPE_COMPANY_A_DAILY:
        refused = _company_a_daily_refusal(schedule, current=current, target_date=target_date)
        if refused:
            return refused
    if source != "worker":
        # The worker reconciles once per cycle; manual "Run now" does it here.
        _reconcile_stale_runs(current)
    if _schedule_requires_company(schedule) and not (schedule.company_key or "").strip():
        _create_event(
            schedule=schedule,
            event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
            message="Schedule is invalid: company is required.",
        )
        return None, RunScheduleEvent.TYPE_SKIPPED_INVALID

    with transaction.atomic():
        schedule = RunSchedule.objects.select_for_update().get(pk=schedule.pk)
        if _schedule_requires_company(schedule) and not (schedule.company_key or "").strip():
            schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
            schedule.last_error = "Company key is required for this schedule."
            schedule.save(update_fields=["last_result", "last_error", "updated_at"])
            _create_event(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
                message="Schedule is invalid: company is required.",
            )
            return None, RunScheduleEvent.TYPE_SKIPPED_INVALID
        if schedule.is_one_time and schedule.completed_at is not None:
            schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
            schedule.last_error = "One-time schedule has already completed."
            schedule.save(update_fields=["last_result", "last_error", "updated_at"])
            _create_event(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
                message="One-time schedule has already completed.",
            )
            return None, RunScheduleEvent.TYPE_SKIPPED_INVALID
        if schedule.is_one_time and schedule.run_once_at is None:
            schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
            schedule.last_error = "Run once time is required for this schedule."
            schedule.save(update_fields=["last_result", "last_error", "updated_at"])
            _create_event(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
                message="Schedule is invalid: run once time is required.",
            )
            return None, RunScheduleEvent.TYPE_SKIPPED_INVALID
        if _is_blocked_company_a_sales_schedule(schedule):
            message = company_a_blocked_message()
            schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
            schedule.last_error = message
            schedule.last_fired_at = current
            schedule.save(update_fields=["last_result", "last_error", "last_fired_at", "updated_at"])
            _create_event(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
                message=message,
            )
            return None, RunScheduleEvent.TYPE_SKIPPED_INVALID
        own_active = _active_scheduled_run(schedule)
        if own_active is not None:
            return _skip_overlap(schedule, current=current, blocking_job=own_active)
        if schedule.scope == RunJob.SCOPE_INVENTORY_PIPELINE:
            global_active, blocking_job, holder = _active_global_run()
            if global_active:
                return _skip_overlap(schedule, current=current, blocking_job=blocking_job, holder=holder)

        payload = _job_payload_from_schedule(schedule, now=current, target_date=target_date)
        job = RunJob.objects.create(**payload)
        schedule.last_result = RunSchedule.LAST_RESULT_QUEUED
        schedule.last_error = ""
        schedule.last_fired_at = current
        update_fields = ["last_result", "last_error", "last_fired_at", "updated_at"]
        if schedule.is_one_time:
            schedule.enabled = False
            schedule.completed_at = current
            schedule.next_fire_at = None
            update_fields.extend(["enabled", "completed_at", "next_fire_at"])
        schedule.save(update_fields=update_fields)

        _create_event(
            schedule=schedule,
            run_job=job,
            event_type=RunScheduleEvent.TYPE_QUEUED,
            message="Run queued.",
            payload={
                "scope": job.scope,
                "company_key": job.company_key,
                "target_date": job.target_date.isoformat() if job.target_date else None,
                "inventory_options": job.inventory_options_json,
            },
        )
        if schedule.is_one_time:
            _create_event(
                schedule=schedule,
                run_job=job,
                event_type=RunScheduleEvent.TYPE_ONE_TIME_COMPLETED,
                message="One-time schedule queued once and was disabled.",
                payload={"completed_at": current.isoformat()},
            )
        return job, RunScheduleEvent.TYPE_QUEUED


def _company_a_daily_refusal(schedule: RunSchedule, *, current: datetime, target_date) -> tuple | None:
    """Nora daily routine gates (workflows module doc): one owner; never replay a completed day."""
    from . import workflows

    canonical = workflows.daily_routine_schedule()
    if (schedule.company_key or "") != workflows.COMPANY_A:
        return _skip(schedule, RunScheduleEvent.TYPE_SKIPPED_INVALID,
                     "A Nora daily routine schedule must have company 'company_a'.", current=current)
    if canonical is not None and canonical.pk != schedule.pk:
        return _skip(schedule, RunScheduleEvent.TYPE_SKIPPED_INVALID,
                     f"Only one Nora daily routine schedule may run ('{canonical.name}'); this duplicate never queues. "
                     "Delete it or pause it.", current=current)
    if not workflows.portal_owns_company_a_daily():
        message = ("The Akponora ops scheduler owns Nora's daily routine "
                   f"({workflows.OWNER_ENV}={workflows.company_a_daily_owner()}); this schedule does not queue it.")
        return _skip(schedule, RunScheduleEvent.TYPE_SKIPPED_NOT_OWNER, message, current=current)
    day = target_date or get_target_trading_date(now=current)
    if workflows.day_already_ran(day):
        return _skip(schedule, RunScheduleEvent.TYPE_SKIPPED_DONE,
                     f"{day.isoformat()} already has a completed daily run; it is not run again automatically.",
                     current=current, payload={"target_date": day.isoformat()})
    return None


def _skip(schedule: RunSchedule, event_type: str, message: str, *, current: datetime, payload=None):
    schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
    schedule.last_error = message
    schedule.last_fired_at = current
    schedule.save(update_fields=["last_result", "last_error", "last_fired_at", "updated_at"])
    _create_event(schedule=schedule, event_type=event_type, message=message, payload=payload or {})
    return None, event_type


def _notify_missed(schedule: RunSchedule, days, due_at: datetime) -> None:
    try:
        from code_scripts.akponora_ops.daily_run import send_summary_slack, short_day

        local = due_at.astimezone(ZoneInfo(schedule.timezone_name or "Africa/Lagos"))
        label = ", ".join(short_day(d.isoformat()) for d in days)
        send_summary_slack(f":red_circle: *Nora Mart · {label}* · daily run missed\n\n"
                           f":hammer_and_wrench: *OIAT* · the scheduler wasn't running from {local:%a %d %b %H:%M}, so "
                           f"{'these days' if len(days) > 1 else 'this day'} did not run automatically. Check, then run "
                           "each from the Inbox (Run daily for date), oldest first.")
    except Exception:  # noqa: BLE001 - a notification problem never changes the schedule
        logger.exception("missed-run notification failed")


def _process_company_a_daily(schedule: RunSchedule, *, now: datetime, due_at: datetime | None):
    """Due fire of Nora's daily routine: missed-run policy, then the normal enqueue with the
    business date bound to the scheduled time."""
    from . import workflows

    try:
        schedule.next_fire_at = schedule.compute_next_fire_at(from_dt=now)
    except Exception as exc:
        return _skip(schedule, RunScheduleEvent.TYPE_SKIPPED_INVALID, f"Schedule is invalid: {exc}", current=now)
    schedule.save(update_fields=["next_fire_at", "updated_at"])
    if not workflows.portal_owns_company_a_daily():
        return enqueue_run_for_schedule(schedule, now=now, source="worker")  # records skipped_not_owner
    fire = due_at or now
    day = workflows.business_date_for_fire(fire)
    if workflows.is_missed(due_at, now):
        # Every fire between the missed one and now, minus days that already ran (e.g. by hand).
        days = [d for d in _fires_between(schedule, fire, now) if not workflows.day_already_ran(d)]
        if not days:
            return _skip(schedule, RunScheduleEvent.TYPE_SKIPPED_DONE,
                         f"Late fire (due {fire.isoformat()}) for days that already ran; nothing to do.", current=now)
        transaction.on_commit(lambda: _notify_missed(schedule, days, fire))  # no HTTP inside the DB transaction
        listed = ", ".join(d.isoformat() for d in days)
        return _skip(schedule, RunScheduleEvent.TYPE_SKIPPED_MISSED,
                     f"Missed: due {fire.isoformat()}, the worker was not running. {listed} not run automatically; "
                     "a person runs each from the Inbox after checking.",
                     current=now, payload={"target_dates": [d.isoformat() for d in days], "target_date": days[0].isoformat(),
                                           "due_at": fire.isoformat()})
    return enqueue_run_for_schedule(schedule, now=now, source="worker", target_date=day)


def _fires_between(schedule: RunSchedule, first: datetime, now: datetime) -> list:
    """Business dates of every scheduled fire from ``first`` up to ``now`` (at most 31)."""
    from . import workflows

    days, at = [], first
    for _ in range(31):
        if at > now:
            break
        days.append(workflows.business_date_for_fire(at))
        try:
            at = schedule.compute_next_fire_at(from_dt=at)
        except Exception:  # noqa: BLE001
            break
    return sorted(set(days))


def _process_due_schedule(schedule: RunSchedule, *, now: datetime) -> tuple[RunJob | None, str]:
    if schedule.scope == RunJob.SCOPE_COMPANY_A_DAILY and not schedule.is_one_time:
        return _process_company_a_daily(schedule, now=now, due_at=schedule.next_fire_at)
    if _schedule_requires_company(schedule) and not (schedule.company_key or "").strip():
        schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
        schedule.last_error = "Company key is required for this schedule."
        schedule.save(update_fields=["last_result", "last_error", "updated_at"])
        _create_event(
            schedule=schedule,
            event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
            message="Schedule is invalid: company is required.",
        )
        return None, RunScheduleEvent.TYPE_SKIPPED_INVALID

    if schedule.is_one_time:
        if schedule.run_once_at is None:
            schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
            schedule.last_error = "Run once time is required for this schedule."
            schedule.save(update_fields=["last_result", "last_error", "updated_at"])
            _create_event(
                schedule=schedule,
                event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
                message="Schedule is invalid: run once time is required.",
            )
            return None, RunScheduleEvent.TYPE_SKIPPED_INVALID
        return enqueue_run_for_schedule(schedule, now=now, source="worker")

    try:
        next_fire_at = schedule.compute_next_fire_at(from_dt=now)
    except Exception as exc:
        schedule.last_result = RunSchedule.LAST_RESULT_SKIPPED_INVALID
        schedule.last_error = str(exc)
        schedule.save(update_fields=["last_result", "last_error", "updated_at"])
        _create_event(
            schedule=schedule,
            event_type=RunScheduleEvent.TYPE_SKIPPED_INVALID,
            message=f"Schedule is invalid: {exc}",
        )
        return None, RunScheduleEvent.TYPE_SKIPPED_INVALID

    schedule.next_fire_at = next_fire_at
    schedule.save(update_fields=["next_fire_at", "updated_at"])
    return enqueue_run_for_schedule(schedule, now=now, source="worker")


def process_schedule_cycle(*, now: datetime | None = None, max_due: int = 25) -> dict[str, int]:
    current = now or timezone.now()
    stats = {
        "initialized": 0,
        "due": 0,
        "queued": 0,
        "skipped_overlap": 0,
        "skipped_invalid": 0,
        "errors": 0,
        "fallback_enabled": 0,
        "fallback_disabled": 0,
        "reconciled": 0,
    }

    # Runs left 'running' by a dead process (e.g. container restart) would make every
    # later schedule skip as "another run is active"; close them before deciding.
    stats["reconciled"] = _reconcile_stale_runs(current)

    fallback_stats = _upsert_env_fallback_schedule(now=current)
    stats.update(fallback_stats)

    for schedule in RunSchedule.objects.filter(enabled=True, next_fire_at__isnull=True):
        if _initialize_missing_next_fire(schedule, now=current):
            stats["initialized"] += 1

    with transaction.atomic():
        due_schedules = list(
            RunSchedule.objects.select_for_update(skip_locked=True)
            .filter(enabled=True, next_fire_at__isnull=False, next_fire_at__lte=current)
            .order_by("next_fire_at", "created_at")[:max_due]
        )
        stats["due"] = len(due_schedules)

        for schedule in due_schedules:
            try:
                job, result = _process_due_schedule(schedule, now=current)
            except Exception as exc:
                stats["errors"] += 1
                logger.exception("Failed processing schedule %s", schedule.id)
                _create_event(
                    schedule=schedule,
                    event_type=RunScheduleEvent.TYPE_ERROR,
                    message=f"Unhandled worker error: {exc}",
                )
                continue

            if job is not None and result == RunScheduleEvent.TYPE_QUEUED:
                stats["queued"] += 1
            elif result == RunScheduleEvent.TYPE_SKIPPED_OVERLAP:
                stats["skipped_overlap"] += 1
            elif result == RunScheduleEvent.TYPE_SKIPPED_INVALID:
                stats["skipped_invalid"] += 1

    # Start queued work every cycle: jobs queued by portal pages / the Inbox are started here when
    # execution is worker-only (job_runner.WORKER_ONLY_ENV).
    if stats["queued"] > 0 or stats["reconciled"] > 0 or RunJob.objects.filter(status=RunJob.STATUS_QUEUED).exists():
        dispatch_next_queued_job()

    _record_heartbeat(current)
    return stats


def _record_heartbeat(now: datetime | None = None) -> None:
    """Update the scheduler worker heartbeat (single row id=1). Used by the Schedules page to show service status."""
    current = now or timezone.now()
    SchedulerWorkerHeartbeat.objects.update_or_create(
        id=1,
        defaults={"last_seen": current},
    )


# Staleness threshold: if last_seen is older than this many seconds, consider the worker "not running"
HEARTBEAT_STALE_MULTIPLIER = 3


def get_scheduler_status() -> dict:
    """
    Return scheduler worker status for the Schedules page.
    Keys: running (bool), last_seen (datetime | None), message (str).
    """
    poll_seconds = configured_poll_seconds()
    stale_seconds = poll_seconds * HEARTBEAT_STALE_MULTIPLIER
    now = timezone.now()

    try:
        hb = SchedulerWorkerHeartbeat.objects.filter(id=1).first()
    except Exception:
        return {"running": False, "last_seen": None, "message": "Scheduler status unavailable."}

    if hb is None:
        return {"running": False, "last_seen": None, "message": "Scheduler has not run yet."}

    age_seconds = (now - hb.last_seen).total_seconds()
    running = age_seconds <= stale_seconds
    if running:
        message = "Worker is polling; scheduled runs will run at their next fire time."
    else:
        message = f"Worker last seen {int(age_seconds)}s ago. Start the scheduler (e.g. docker compose up -d scheduler) for scheduled runs to execute."
    return {"running": running, "last_seen": hb.last_seen, "message": message}
