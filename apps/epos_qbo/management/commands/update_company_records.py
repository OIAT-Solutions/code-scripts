import json
import subprocess
from django.core.management.base import BaseCommand,CommandError
from apps.epos_qbo.models import RunJob,CompanyConfigRecord
from apps.epos_qbo.services import workspace_jobs,workspace_records as records


class Command(BaseCommand):
    def add_arguments(self,parser):parser.add_argument("job_id")

    def handle(self,*args,**options):
        job=RunJob.objects.get(pk=options["job_id"])
        user=job.requested_by
        if job.scope!=RunJob.SCOPE_WORKSPACE_READ or job.company_key!="company_a" or not user or not user.is_active or not user.has_perm("epos_qbo.can_trigger_runs"):
            raise CommandError("This records update is no longer authorised")
        if not CompanyConfigRecord.objects.filter(company_key="company_a",is_active=True).exists():raise CommandError("Company is inactive")
        opts=job.inventory_options_json;action=opts.get("action")
        cmd=workspace_jobs.command(action,job.id,opts.get("day",""))
        if action=="deposit_status":
            folder=records.read_root()/str(job.id);folder.mkdir(parents=True,exist_ok=True)
            path=folder/"status.json"
            with path.open("w") as f:code=subprocess.call(cmd,stdout=f)
            if code:raise CommandError("The till-sheet check did not finish. Previous records remain available.")
            data=records.document(path)
            if not isinstance(data.get("till_sheet"),dict) or not isinstance(data.get("days"),dict):raise CommandError("The till-sheet report is incomplete")
            pointer=records.read_root()/"latest_deposit_status.json";temp=pointer.with_suffix(".tmp")
            temp.write_text(json.dumps(data));temp.replace(pointer)
            self.stdout.write(data["till_sheet"].get("text","Till-sheet status updated"))
        else:
            code=subprocess.call(cmd)
            if code==3 and action=="deposit_plan":self.stdout.write("Plan saved; this day needs review.")
            elif code:raise CommandError("Another stock update is running. Try again when it finishes." if code==5 and action=="stock" else "The records update did not finish. Open supporting details.")
            else:self.stdout.write("Stock snapshot updated." if action=="stock" else "Deposit plan saved. Nothing was banked.")
