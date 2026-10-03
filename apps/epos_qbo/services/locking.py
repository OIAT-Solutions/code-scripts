from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import RunJob, RunLock


def clear_if_stale(lock: RunLock) -> bool:
    """Release a lock whose owning job has already finished (succeeded / failed / cancelled).

    3 Oct 2026: a stuck job was marked failed by hand without releasing the lock, and every later
    portal job (the Goldplates schedule, Inbox approvals) stayed queued. Call inside a
    select_for_update transaction. Returns True when it released the lock."""
    if lock.active and lock.owner_run_job and lock.owner_run_job.status not in (
        RunJob.STATUS_QUEUED,
        RunJob.STATUS_RUNNING,
    ):
        lock.active = False
        lock.holder = ""
        lock.owner_run_job = None
        lock.acquired_at = None
        lock.save()
        return True
    return False


def acquire_run_lock(*, holder: str, run_job: RunJob | None = None) -> tuple[bool, str]:
    with transaction.atomic():
        lock, _ = RunLock.objects.select_for_update().get_or_create(id=1)
        clear_if_stale(lock)
        if lock.active:
            return False, f"Lock held by {lock.holder or 'unknown'}"
        lock.active = True
        lock.holder = holder
        lock.owner_run_job = run_job
        lock.acquired_at = timezone.now()
        lock.save()
    return True, "acquired"


def release_run_lock(*, run_job: RunJob | None = None, force: bool = False) -> None:
    with transaction.atomic():
        lock, _ = RunLock.objects.select_for_update().get_or_create(id=1)
        if not lock.active:
            return
        if force or run_job is None or lock.owner_run_job_id == getattr(run_job, "id", None):
            lock.active = False
            lock.holder = ""
            lock.owner_run_job = None
            lock.acquired_at = None
            lock.save()
