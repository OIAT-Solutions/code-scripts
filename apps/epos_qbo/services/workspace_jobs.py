"""Only documented read-only operations; no caller-supplied commands or paths."""
import sys
from datetime import date
from . import workspace_records as records


def command(action,job_id,day=""):
    if action=="stock":return [sys.executable,"-m","code_scripts.akponora_ops.stock_snapshot","run"]
    if action=="deposit_status":return [sys.executable,"-m","code_scripts.akponora_ops.uf_deposits","status","--json"]
    if action=="deposit_plan":
        from code_scripts.akponora_ops.daily_run import last_closed_business_date
        parsed=date.fromisoformat(day)
        if not date(2026,9,25)<=parsed<=last_closed_business_date():raise ValueError("Choose a closed day from 25 September 2026")
        return [sys.executable,"-m","code_scripts.akponora_ops.uf_deposits","plan","--from",day,"--to",day,"--out",str(records.read_root()/str(job_id)/"deposits"),"--no-slack"]
    raise ValueError("Unsupported records update")
