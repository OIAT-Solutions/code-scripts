"""Detect and close RunJobs stuck in ``running`` after their process has gone.

A dashboard/scheduler run is a subprocess; its monitor thread flips the RunJob
out of ``running`` when it exits. If the container restarts mid-run, nothing
reports back and the job stays ``running`` forever, which makes the scheduler
skip every later run ("another run is active").

Checking only the stored PID is not enough: inside Docker, PIDs restart from 1
after a container restart, so an unrelated process can own the old PID. The
signals used here, strongest first:

1. Max runtime: running longer than ``OIAT_RUNJOB_MAX_HOURS`` (default 6) -> stale.
2. Global flock (code_scripts/run_lock.py): sales runs (single / all companies)
   always hold it while alive and the OS drops it when they die. If it is free
   after a short start-up grace period, the run is gone.
3. PID: missing, not alive, or (Linux) a process that started after the job did
   (PID reused). Because a PID can only be checked from the container that
   spawned it, this signal is ignored while the job's log file is still being
   written, and a job with no stored PID is left to rules 1 and 2.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from code_scripts.run_lock import global_lock_is_free

from ..models import RunJob, RunLock, RunSchedule, RunScheduleEvent

logger = logging.getLogger(__name__)

DEFAULT_MAX_HOURS = 6.0
# Time a fresh subprocess gets to start Python and take the global flock.
LOCK_GRACE = timedelta(minutes=5)
# A log written to within this window means the run is alive somewhere.
LOG_ACTIVE_WINDOW = timedelta(minutes=15)
# Slack between job.started_at and the real process start time.
PID_START_TOLERANCE = timedelta(minutes=2)
# Scopes whose subprocess always holds the global flock for its whole life.
FLOCK_SCOPES = frozenset({RunJob.SCOPE_SINGLE, RunJob.SCOPE_ALL, RunJob.SCOPE_COMPANY_A_DAILY})


def max_runtime_hours() -> float:
    raw = os.getenv("OIAT_RUNJOB_MAX_HOURS")
    if raw is None or not raw.strip():
        return DEFAULT_MAX_HOURS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_HOURS
    return value if value > 0 else DEFAULT_MAX_HOURS


def job_start_time(job: RunJob) -> datetime:
    return job.started_at or job.dispatched_at or job.created_at or timezone.now()


def format_duration(delta: timedelta) -> str:
    total_minutes = max(0, int(delta.total_seconds() // 60))
    days, rem = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def describe_run(job: RunJob, *, now: datetime | None = None) -> str:
    """One-line description of a run for operator-facing messages."""
    current = now or timezone.now()
    what = job.get_scope_display()
    if job.company_key:
        what = f"{what} ({job.company_key})"
    started = job_start_time(job)
    started_local = timezone.localtime(started).strftime("%Y-%m-%d %H:%M")
    return (
        f"{what} run {job.friendly_id}, {job.status} for {format_duration(current - started)} "
        f"(since {started_local}; id {job.id})"
    )


def describe_run_short(job: RunJob, *, now: datetime | None = None) -> str:
    """Compact form for narrow UI fields (schedule last_error is truncated at 80 chars)."""
    current = now or timezone.now()
    company = f" ({job.company_key})" if job.company_key else ""
    return f"Blocked by {job.friendly_id}{company}, {job.status} for {format_duration(current - job_start_time(job))}"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start_time(pid: int) -> datetime | None:
    """Wall-clock start of ``pid`` from /proc (Linux); None when unavailable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 2 :].split()
        start_ticks = int(fields[19])  # field 22 of /proc/<pid>/stat
        btime = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None
        ticks = os.sysconf("SC_CLK_TCK")
        return datetime.fromtimestamp(btime + start_ticks / ticks, tz=dt_timezone.utc)
    except (OSError, ValueError, IndexError):
        return None


def _log_recently_written(job: RunJob, now: datetime) -> bool:
    if not job.log_file_path:
        return False
    try:
        mtime = Path(os.path.expandvars(job.log_file_path)).expanduser().stat().st_mtime
    except OSError:
        return False
    age_seconds = time.time() - mtime
    return age_seconds < LOG_ACTIVE_WINDOW.total_seconds()


@dataclass
class StaleVerdict:
    stale: bool
    reason: str = ""


def assess_running_job(job: RunJob, *, now: datetime, lock_free: bool | None) -> StaleVerdict:
    started = job_start_time(job)
    age = now - started
    max_hours = max_runtime_hours()
    if age > timedelta(hours=max_hours):
        return StaleVerdict(
            True,
            f"it was still marked running after {format_duration(age)}, past the "
            f"{max_hours:g}-hour safety limit (OIAT_RUNJOB_MAX_HOURS)",
        )

    if job.scope in FLOCK_SCOPES and lock_free is True and age > LOCK_GRACE:
        return StaleVerdict(
            True,
            "the global run lock was free, so no pipeline process was running "
            f"(stored PID {job.pid or 'none'} is not this run)",
        )

    if job.scope == RunJob.SCOPE_COMPANY_A_DAILY and lock_free is False:
        # daily_run holds the global flock for its whole run and writes its step output to its evidence
        # folders, not the job log. The PID may live in another container (the worker), so a held lock
        # is the authoritative "still running" signal for this scope.
        return StaleVerdict(False)

    if _log_recently_written(job, now):
        return StaleVerdict(False)

    if not job.pid:
        # Nothing to check; only the lock and max-runtime rules apply.
        return StaleVerdict(False)
    if not _pid_alive(job.pid):
        return StaleVerdict(True, f"its process (PID {job.pid}) no longer exists")
    proc_start = process_start_time(job.pid)
    if proc_start is not None and proc_start > started + PID_START_TOLERANCE:
        return StaleVerdict(
            True,
            f"PID {job.pid} now belongs to a different process that started "
            f"{timezone.localtime(proc_start):%Y-%m-%d %H:%M}, after the run began",
        )
    return StaleVerdict(False)


def _mark_failed(job_id, *, reason: str, now: datetime) -> RunJob | None:
    detected = timezone.localtime(now).strftime("%Y-%m-%d %H:%M")
    headline = f"Marked failed: the run stopped without reporting back (detected {detected})."
    with transaction.atomic():
        job = RunJob.objects.select_for_update().filter(id=job_id, status=RunJob.STATUS_RUNNING).first()
        if job is None:
            return None
        job.status = RunJob.STATUS_FAILED
        job.exit_code = -1
        job.finished_at = now
        job.failure_reason = f"{headline} Reason: {reason}."
        job.save(update_fields=["status", "exit_code", "finished_at", "failure_reason"])

        lock = RunLock.objects.select_for_update().filter(id=1).first()
        if lock is not None and lock.active and lock.owner_run_job_id in (None, job.id):
            lock.active = False
            lock.holder = ""
            lock.owner_run_job = None
            lock.acquired_at = None
            lock.save()

        schedule = job.scheduled_by
        payload = {"reconciled": True, "reason": reason, "status": job.status, "exit_code": -1}
        if schedule is not None:
            schedule.last_result = RunSchedule.LAST_RESULT_FAILED
            schedule.last_error = job.failure_reason
            schedule.save(update_fields=["last_result", "last_error", "updated_at"])
            payload["schedule_id"] = str(schedule.id)
            payload["schedule_name"] = schedule.name
        RunScheduleEvent.objects.create(
            schedule=schedule,
            run_job=job,
            event_type=RunScheduleEvent.TYPE_RUN_FAILED,
            message=job.failure_reason,
            payload_json=payload,
        )
    logger.warning("Reconciled stuck RunJob %s: %s", job.id, reason)
    return job


def reconcile_stale_running_jobs(*, now: datetime | None = None) -> list[RunJob]:
    """Mark every stale ``running`` job failed. Returns the jobs it closed."""
    current = now or timezone.now()
    running = list(RunJob.objects.filter(status=RunJob.STATUS_RUNNING).order_by("created_at"))
    if not running:
        return []
    needs_probe = any(
        job.scope in FLOCK_SCOPES and current - job_start_time(job) > LOCK_GRACE for job in running
    )
    lock_free = global_lock_is_free() if needs_probe else None

    closed: list[RunJob] = []
    for job in running:
        verdict = assess_running_job(job, now=current, lock_free=lock_free)
        if not verdict.stale:
            continue
        marked = _mark_failed(job.id, reason=verdict.reason, now=current)
        if marked is not None:
            closed.append(marked)
    return closed
