"""Background entry point for read-only records updates queued from the company tabs."""
import json
import subprocess

from django.core.management.base import BaseCommand, CommandError

from apps.epos_qbo.models import CompanyConfigRecord, RunJob
from apps.epos_qbo.services import workspace_jobs, workspace_records as records


class Command(BaseCommand):
    help = "Run one queued Company A records update (stock snapshot, till-sheet status, deposit re-plan)."

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        job = RunJob.objects.get(pk=options["job_id"])
        user = job.requested_by
        if job.scope != RunJob.SCOPE_WORKSPACE_READ or job.company_key != "company_a" or not user \
                or not user.is_active or not user.has_perm("epos_qbo.can_trigger_runs"):
            raise CommandError("This records update is no longer authorised")
        if not CompanyConfigRecord.objects.filter(company_key="company_a", is_active=True).exists():
            raise CommandError("Company is inactive")
        opts = job.inventory_options_json or {}
        action = opts.get("action")
        cmd = workspace_jobs.command(action, job.id, opts.get("day", ""), opts.get("mode", ""))
        if action == "deposit_status":
            self.deposit_status(job, cmd)
            return
        code = subprocess.call(cmd)
        if code == 3 and action == "deposit_plan":
            self.stdout.write("Plan saved; this day needs review. Nothing was banked.")
        elif code == 5 and action == "stock":
            raise CommandError("Another stock check is already running. Try again when it finishes.")
        elif code:
            raise CommandError("The update did not finish. The previous records stay on the page. See the log above.")
        else:
            self.stdout.write({"stock": "Stock check saved.", "recheck": "Bills and banking re-checked. Nothing was posted."}
                              .get(action, "Deposit plan saved. Nothing was banked."))

    def deposit_status(self, job, cmd):
        folder = records.read_root() / str(job.id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "status.json"
        with path.open("w") as handle:
            code = subprocess.call(cmd, stdout=handle)
        if code:
            raise CommandError("The till-sheet check did not finish. The previous status stays on the page.")
        data = records.document(path)
        if not isinstance(data.get("till_sheet"), dict) or not isinstance(data.get("days"), dict):
            raise CommandError("The till-sheet report is incomplete")
        pointer = records.read_root() / "latest_deposit_status.json"
        temp = pointer.with_suffix(".tmp")
        temp.write_text(json.dumps(data))
        temp.replace(pointer)
        self.stdout.write(data["till_sheet"].get("text", "Till-sheet status updated"))
