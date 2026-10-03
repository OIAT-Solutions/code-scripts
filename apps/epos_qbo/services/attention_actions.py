"""Portal adapters to existing tools; executed only by a background RunJob."""
from __future__ import annotations

import csv
import subprocess
import sys
from datetime import date

from . import attention, company_a_ops as ops


class ReviewChanged(ValueError):
    pass


def current_item(key, expected):
    items, errors = attention.inbox()
    item = next((i for i in items if i["key"] == key), None)
    if item is None or item["snapshot"] != expected:
        raise ReviewChanged("The evidence changed. Refresh the inbox and review it again.")
    return item


def tool_command(item, action, approval_ref):
    if item["kind"] == "hold":
        raise ValueError("Hold clear uses the existing hold function")
    folder = ops.resolve_evidence(item["run"], item["relative"] + "/summary.json").parent
    if item["kind"] == "bill":
        return [sys.executable, "-m", "code_scripts.akponora_ops.bills_sync", "post", "--review", str(folder / "review.csv"), "--approval-ref", approval_ref, "--expect-sha", item["extra"]["sha"], "--no-slack"]
    if item["kind"] == "catalogue":
        return [sys.executable, "-m", "code_scripts.akponora_ops.catalogue_sync", "apply", "--plan-dir", str(folder), "--approval-ref", approval_ref, "--expect-plan-sha", item["extra"]["sha"], "--no-slack"]
    if item["kind"] == "deposit":
        return [sys.executable, "-m", "code_scripts.akponora_ops.uf_deposits", "post", "--plan-dir", str(folder), "--approval-ref", approval_ref, "--expect-sha", item["extra"]["sha"], "--no-slack"]
    raise ValueError("This tool does not support manual approval or skipping. Fix the source and re-plan.")


def execute(action_record):
    """Called with the shared pipeline lock held. Revalidate at execution, not just enqueue."""
    data = action_record.payload
    ref = f"{action_record.actor} via portal {action_record.created_at.isoformat()}; chat approval {data.get('approval_ref', '')}; {action_record.reason}"
    if action_record.action == "daily":
        target = date.fromisoformat(data["date"])
        from code_scripts.akponora_ops.daily_run import last_closed_business_date
        # Date was bound at confirmation: never let a queued job roll to another day.
        if target > last_closed_business_date() or target < date(2026, 10, 1):
            raise ValueError("Choose a closed business day from 1 October 2026.")
        cmd = [sys.executable, "-m", "code_scripts.akponora_ops.daily_run", "--date", target.isoformat(), "--no-slack"]
        if data["dry_run"]:
            cmd.append("--dry-run")
        if data.get("only"):
            cmd.extend(["--only", data["only"]])
        return subprocess.call(cmd)
    item = current_item(data["key"], data["snapshot"])
    if action_record.action == "approve" and not item["approve"]:
        raise ValueError("This item is held. Fix the cause and make a new plan.")
    if action_record.action == "skip" and not item["skip"]:
        raise ValueError("Skipping this item is not supported by the existing tool.")
    if action_record.action == "skip" and item["kind"] != "bill":
        # Deferral records a human decision only; it never advances a financial cursor.
        return 0
    if item["kind"] == "vendor":
        chosen = next((c for c in item["extra"]["candidates"] if c["id"] == data.get("vendor_id")), None)
        if not chosen:
            raise ValueError("Choose one of the reviewed existing supplier matches")
        from code_scripts.akponora_ops import vendors
        from code_scripts.scripts.akponora_cutover.w7_create_items import QBOClient
        client = QBOClient.for_company_a(allow_writes=False)
        live = client.get_json(f"/vendor/{chosen['id']}").get("Vendor") or {}
        if live.get("DisplayName") != chosen["name"] or live.get("Active") is False:
            raise ValueError("Supplier changed or is inactive. Re-plan and review again.")
        vendor = item["extra"]["vendor"]
        path = ops.state_root() / "mappings/company_a/vendors.csv"
        vendors.append_vendor_rows(path, [{"EPOS Supplier Id": vendor.get("supplier_id", ""), "EPOS Supplier Name": vendor["epos_name"], "QBO Vendor Id": chosen["id"], "QBO Vendor Name": chosen["name"], "Approved By": ref}])
        return 0
    if item["kind"] == "hold":
        from code_scripts.operations_controls import clear_posting_hold
        clear_posting_hold(approved_by=action_record.actor, reason=ref)
        return 0
    if item["kind"] == "bill":
        folder = ops.resolve_evidence(item["run"], "bills/review.csv").parent
        path = folder / "review.csv"
        review = attention.rows(path)
        # Clear every other approval so this job authorizes exactly the selected PO.
        for row in review:
            row["Approve"] = ("yes" if action_record.action == "approve" else "skip") if row["PO"] == item["identity"] else ""
        temporary = path.with_suffix(".portal.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(review[0]))
            writer.writeheader()
            writer.writerows(review)
        temporary.replace(path)
    return subprocess.call(tool_command(item, action_record.action, ref))
