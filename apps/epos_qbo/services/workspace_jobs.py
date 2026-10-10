"""Only documented read-only operations; no caller-supplied commands or paths.

* ``stock``          -> ``stock_snapshot run`` (EPOS + QuickBooks reads; ``mode=qbo`` adds ``--no-epos``)
* ``deposit_status`` -> ``uf_deposits status --json`` (till sheet + days.json; no QuickBooks)
* ``deposit_plan``   -> ``uf_deposits plan`` for one closed day into a portal scratch folder
* ``recheck``        -> ``recheck`` (bills + banking plans) into a portal scratch folder; the inbox reads it
"""
import sys
from datetime import date

from . import workspace_records as records

STOCK_MODES = {"": [], "qbo": ["--no-epos"], "epos": ["--no-qbo"]}
DEPOSIT_FLOOR = date(2026, 9, 25)


def command(action, job_id, day="", mode=""):
    if action == "stock":
        if mode not in STOCK_MODES:
            raise ValueError("Unsupported stock update")
        return [sys.executable, "-m", "code_scripts.akponora_ops.stock_snapshot", "run", *STOCK_MODES[mode]]
    if action == "deposit_status":
        return [sys.executable, "-m", "code_scripts.akponora_ops.uf_deposits", "status", "--json"]
    if action == "deposit_plan":
        from code_scripts.akponora_ops.daily_run import last_closed_business_date
        try:
            parsed = date.fromisoformat(day)
        except (TypeError, ValueError):
            raise ValueError("Choose a valid day") from None
        if not DEPOSIT_FLOOR <= parsed <= last_closed_business_date():
            raise ValueError("Choose a closed day from 25 September 2026")
        return [sys.executable, "-m", "code_scripts.akponora_ops.uf_deposits", "plan", "--from", day, "--to", day,
                "--out", str(records.read_root() / str(job_id) / "deposits"), "--no-slack"]
    if action == "recheck":
        return [sys.executable, "-m", "code_scripts.akponora_ops.recheck", "--out", str(records.read_root() / str(job_id))]
    raise ValueError("Unsupported records update")
