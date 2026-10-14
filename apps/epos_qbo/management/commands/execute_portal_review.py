"""Background entry point; never run a review inside an HTTP request."""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.epos_qbo.models import PortalReviewAction, RunJob
from apps.epos_qbo.services.attention_actions import execute


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        record = PortalReviewAction.objects.get(job_id=options["job_id"])
        if record.job.scope != RunJob.SCOPE_PORTAL_REVIEW or record.finished_at:
            raise CommandError("Review action is invalid or already completed")
        try:
            if record.action == "daily":
                # daily_run acquires this same file lock itself.
                code = execute(record)
            else:
                from code_scripts.run_lock import hold_global_lock
                with hold_global_lock(holder=f"portal-review:{record.job_id}") as lock:
                    if not lock.acquired:
                        raise CommandError("Another pipeline run is active. Wait for it to finish and retry.")
                    code = execute(record)
            record.result = "Completed" if code == 0 else ("Run finished; items still need review" if code == 3 else f"Tool stopped (exit {code}); review the job details")
            if code not in (0, 3) or (code == 3 and record.action != "daily"):
                raise CommandError(record.result)
        except Exception as exc:
            record.result = f"Stopped: {exc}"
            raise CommandError(str(exc)) from exc
        finally:
            record.finished_at = timezone.now()
            record.save(update_fields=["result", "finished_at"])
        self.stdout.write(record.result)
