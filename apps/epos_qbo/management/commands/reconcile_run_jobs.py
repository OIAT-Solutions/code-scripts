from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.epos_qbo.services.run_reconciler import reconcile_stale_running_jobs


class Command(BaseCommand):
    help = (
        "Mark stuck running jobs as failed: the global run lock is free (sales runs), "
        "the PID is gone or reused, or the run exceeded OIAT_RUNJOB_MAX_HOURS. "
        "The schedule worker also runs this automatically every cycle."
    )

    def handle(self, *args, **options):
        closed = reconcile_stale_running_jobs()
        for job in closed:
            self.stdout.write(f"{job.id}: {job.failure_reason}")
        self.stdout.write(self.style.SUCCESS(f"Reconciled {len(closed)} run job(s)."))
