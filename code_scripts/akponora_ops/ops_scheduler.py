"""Cron runner for the company_a operations jobs.

Separate from the portal's sales scheduler (``manage.py run_schedule_worker``). A job is
scheduled only when it is switched on, so an unconfigured server runs nothing.

The single scheduled Company A job (recommended):

  OIAT_COMPANY_A_DAILY_RUN_ENABLED=1   run ``daily_run`` (catalogue -> bills -> sales -> guard)
  OIAT_COMPANY_A_DAILY_RUN_CRON        default "0 6 * * *" (SCHEDULE_TZ, default Africa/Lagos)

While daily_run is on, the portal schedule worker never schedules Company A sales and the
individual job crons below are IGNORED (no double runs) unless
``OIAT_AKPONORA_ALLOW_INDIVIDUAL_CRONS=1`` is also set. daily_run takes the global run lock itself.

Individual jobs (each still runnable alone with ``--run-now`` or its own module):

  OIAT_AKPONORA_CATALOGUE_SYNC_CRON  e.g. "30 6 * * *"
  OIAT_AKPONORA_BILLS_SYNC_CRON      e.g. "0 9,15 * * *"
  OIAT_AKPONORA_ITEM_GUARD_CRON      e.g. "0 8 * * *"
  OIAT_AKPONORA_<JOB>_CMD            optional command override
  SCHEDULE_TZ                        default Africa/Lagos

Jobs that can write to QBO take the global run lock and are skipped (not queued) while a
sales run holds it; they only write when their own automation env flags are on.

Run: python -m code_scripts.akponora_ops.ops_scheduler [--list] [--run-now JOB]
"""
from __future__ import annotations

import argparse
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from code_scripts.akponora_ops.common import REPO_ROOT, env_flag, send_slack
from code_scripts.load_env import load_env_file
from code_scripts.run_lock import hold_global_lock

LOGGER = logging.getLogger("oiat.akponora_ops")
DEFAULT_TZ = "Africa/Lagos"


DAILY_RUN = "daily_run"
DAILY_ENABLED_ENV = "OIAT_COMPANY_A_DAILY_RUN_ENABLED"
DAILY_CRON_ENV = "OIAT_COMPANY_A_DAILY_RUN_CRON"
DAILY_DEFAULT_CRON = "0 6 * * *"
ALLOW_INDIVIDUAL_ENV = "OIAT_AKPONORA_ALLOW_INDIVIDUAL_CRONS"


@dataclass(frozen=True)
class OpsJob:
    name: str
    default_args: tuple[str, ...]
    takes_lock: bool
    self_reports: bool = False  # sends its own Slack summary (no extra failure alert)

    @property
    def env_prefix(self) -> str:
        if self.name == DAILY_RUN:
            return "OIAT_COMPANY_A_DAILY_RUN"
        return f"OIAT_AKPONORA_{self.name.upper()}"

    def cron(self) -> str:
        if self.name == DAILY_RUN:
            if not daily_run_enabled():
                return ""
            return os.getenv(DAILY_CRON_ENV, "").strip() or DAILY_DEFAULT_CRON
        return os.getenv(f"{self.env_prefix}_CRON", "").strip()

    def command(self) -> list[str]:
        override = os.getenv(f"{self.env_prefix}_CMD", "").strip()
        if override:
            return shlex.split(override)
        return [sys.executable, "-m", f"code_scripts.akponora_ops.{self.name}", *self.default_args]


JOBS = {
    job.name: job
    for job in (
        OpsJob("catalogue_sync", ("scheduled",), takes_lock=True),
        OpsJob("bills_sync", ("scheduled",), takes_lock=True),
        OpsJob("item_guard", ("--fail-on-alert",), takes_lock=False),
        # daily_run holds the global lock itself (and hands it to its children).
        OpsJob(DAILY_RUN, (), takes_lock=False, self_reports=True),
    )
}


def daily_run_enabled() -> bool:
    return env_flag(DAILY_ENABLED_ENV)


def run_job(job: OpsJob) -> int:
    cmd = job.command()
    LOGGER.info("Running %s: %s", job.name, shlex.join(cmd))
    if job.takes_lock:
        with hold_global_lock(f"akponora_ops:{job.name}") as lock:
            if not lock.acquired:
                LOGGER.warning("Skipped %s: run lock held (%s)", job.name, lock.reason)
                return 2
            return _execute(job, cmd)
    return _execute(job, cmd)


def _execute(job: OpsJob, cmd: list[str]) -> int:
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=dict(os.environ), check=False)
    LOGGER.info("%s exited with code %s", job.name, proc.returncode)
    if proc.returncode not in (0, 3, 4) and not job.self_reports:
        send_slack(f":warning: Akponora {job.name} job failed (exit {proc.returncode}). Check the ops scheduler log.")
    return proc.returncode


def configured_jobs() -> list[OpsJob]:
    """Jobs to schedule. With daily_run on, it is the only one unless individual crons are
    explicitly allowed (so the same step never runs twice a day by accident)."""
    jobs = [job for job in JOBS.values() if job.cron()]
    if daily_run_enabled() and not env_flag(ALLOW_INDIVIDUAL_ENV):
        ignored = [job.name for job in jobs if job.name != DAILY_RUN]
        if ignored:
            LOGGER.warning("daily_run is on: ignoring individual crons for %s (set %s=1 to keep them)",
                           ", ".join(ignored), ALLOW_INDIVIDUAL_ENV)
        jobs = [job for job in jobs if job.name == DAILY_RUN]
    return jobs


def next_fire_times(jobs: list[OpsJob], now: datetime) -> dict[str, datetime]:
    return {job.name: croniter(job.cron(), now).get_next(datetime) for job in jobs}


def run_scheduler(*, sleep=time.sleep, now=None, max_cycles: int | None = None, idle_when_empty: bool = False) -> int:
    """Run due jobs one at a time (never concurrently); a job missed while another ran fires once."""
    tz = ZoneInfo(os.getenv("SCHEDULE_TZ", "").strip() or DEFAULT_TZ)
    clock = now or (lambda: datetime.now(tz))
    jobs = configured_jobs()
    if not jobs:
        LOGGER.error("No ops job configured (set %s=1 or OIAT_AKPONORA_<JOB>_CRON).", DAILY_ENABLED_ENV)
        if not idle_when_empty:
            return 1
        # container mode: stay up quietly instead of restart-looping; env changes need a restart
        while max_cycles is None:
            sleep(3600)
        return 1
    for job in jobs:
        if not croniter.is_valid(job.cron()):
            LOGGER.error("Invalid cron for %s: '%s'", job.name, job.cron())
            return 1
        LOGGER.info("Scheduled %s at '%s' (%s)", job.name, job.cron(), tz)
    due = next_fire_times(jobs, clock())
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        name = min(due, key=due.get)
        wait = (due[name] - clock()).total_seconds()
        if wait > 0:
            sleep(min(wait, 60))
            continue
        try:
            run_job(JOBS[name])
        except Exception:
            LOGGER.exception("%s crashed", name)
        due[name] = croniter(JOBS[name].cron(), clock()).get_next(datetime)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the configured jobs and exit")
    parser.add_argument("--run-now", choices=sorted(JOBS), help="run one job immediately and exit")
    args = parser.parse_args(argv)
    load_env_file()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    if args.list:
        active = {job.name for job in configured_jobs()}
        for job in JOBS.values():
            state = job.cron() or "(off)"
            if job.cron() and job.name not in active:
                state += " (ignored: daily_run on)"
            print(f"{job.name:15} cron={state:20} cmd={shlex.join(job.command())}")
        return 0
    if args.run_now:
        return run_job(JOBS[args.run_now])
    return run_scheduler(idle_when_empty=True)


if __name__ == "__main__":
    raise SystemExit(main())
