"""Cron runner for the company_a operations jobs (catalogue sync, bills sync, item guard).

Separate from the portal's sales scheduler (``manage.py run_schedule_worker``). A job is
scheduled only when its cron env var is set, so an unconfigured server runs nothing.
Jobs that can write to QBO take the global run lock and are skipped (not queued) while a
sales run holds it; they only write when their own automation env flags are on.

  OIAT_AKPONORA_CATALOGUE_SYNC_CRON  e.g. "30 6 * * *"
  OIAT_AKPONORA_BILLS_SYNC_CRON      e.g. "0 9,15 * * *"
  OIAT_AKPONORA_ITEM_GUARD_CRON      e.g. "0 8 * * *"
  OIAT_AKPONORA_<JOB>_CMD            optional command override
  SCHEDULE_TZ                        default Africa/Lagos

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

from code_scripts.akponora_ops.common import REPO_ROOT, send_slack
from code_scripts.load_env import load_env_file
from code_scripts.run_lock import hold_global_lock

LOGGER = logging.getLogger("oiat.akponora_ops")
DEFAULT_TZ = "Africa/Lagos"


@dataclass(frozen=True)
class OpsJob:
    name: str
    default_args: tuple[str, ...]
    takes_lock: bool

    @property
    def env_prefix(self) -> str:
        return f"OIAT_AKPONORA_{self.name.upper()}"

    def cron(self) -> str:
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
    )
}


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
    if proc.returncode not in (0, 3, 4):
        send_slack(f":warning: Akponora {job.name} job failed (exit {proc.returncode}). Check the ops scheduler log.")
    return proc.returncode


def configured_jobs() -> list[OpsJob]:
    return [job for job in JOBS.values() if job.cron()]


def next_fire_times(jobs: list[OpsJob], now: datetime) -> dict[str, datetime]:
    return {job.name: croniter(job.cron(), now).get_next(datetime) for job in jobs}


def run_scheduler(*, sleep=time.sleep, now=None, max_cycles: int | None = None) -> int:
    """Run due jobs one at a time (never concurrently); a job missed while another ran fires once."""
    tz = ZoneInfo(os.getenv("SCHEDULE_TZ", "").strip() or DEFAULT_TZ)
    clock = now or (lambda: datetime.now(tz))
    jobs = configured_jobs()
    if not jobs:
        LOGGER.error("No ops job configured (set OIAT_AKPONORA_<JOB>_CRON). Exiting.")
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
        for job in JOBS.values():
            print(f"{job.name:15} cron={job.cron() or '(off)':20} cmd={shlex.join(job.command())}")
        return 0
    if args.run_now:
        return run_job(JOBS[args.run_now])
    return run_scheduler()


if __name__ == "__main__":
    raise SystemExit(main())
