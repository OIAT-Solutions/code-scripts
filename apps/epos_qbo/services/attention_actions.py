"""Portal adapters to existing tools; executed only by a background RunJob.

Every write goes through the pipeline's own command-line tool with its gates (approval reference,
plan / payload SHA, per-decision SHA). Nothing here talks to QuickBooks itself.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from . import attention, company_a_ops as ops

LAGOS = ZoneInfo("Africa/Lagos")
TOOLS = "code_scripts.akponora_ops."


class ReviewChanged(ValueError):
    pass


def actor_name(user) -> str:
    return (user.get_full_name() or "").strip() or user.get_username()


def approval_reference(user, when: datetime | None = None) -> str:
    """Filled automatically; staff never type a chat reference."""
    when = (when or timezone.now()).astimezone(LAGOS)
    return f"Approved by {actor_name(user)} in the portal, {when:%Y-%m-%d %H:%M} Lagos"


def current_item(key, expected):
    items, errors = attention.inbox()
    item = next((i for i in items if i["key"] == key), None)
    if item is None or item["snapshot"] != expected:
        raise ReviewChanged("The evidence changed. Refresh the inbox and review it again.")
    return item


def _tool(name):
    return [sys.executable, "-m", TOOLS + name]


def _folder(item):
    return ops.resolve_evidence(item["run"], item["relative"] + "/summary.json").parent


def product_command(item, approval_ref):
    extra = item["extra"]
    selected = []
    if extra.get("with_master"):
        selected.append((extra["with_master"]["pid"], extra["with_master"]["decision_sha"]))
    selected.append((extra["pid"], extra["decision_sha"]))
    return _tool("catalogue_sync") + [
        "apply", "--plan-dir", str(_folder(item)), "--approval-ref", approval_ref, "--expect-sha", extra["sha"],
        "--only", ",".join(pid for pid, _ in selected),
        "--expect-decision-shas", ",".join(f"{pid}={sha}" for pid, sha in selected),
        "--json", "--no-slack"]


def vendor_command(item, approval_ref, choice, *, dry_run, expect_sha=""):
    vendor = item["extra"]["vendor"]
    cmd = _tool("vendors") + ["approve", "--epos-name", vendor.get("epos_name", "")]
    if choice.get("mode") == "create":
        cmd.append("--create")
        if choice.get("display_name"):
            cmd += ["--display-name", choice["display_name"]]
    else:
        cmd += ["--link-to", choice["link_to"]]
    if vendor.get("supplier_id"):
        cmd += ["--epos-supplier-id", str(vendor["supplier_id"])]
    cmd += ["--approval-ref", approval_ref]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd += ["--expect-sha", expect_sha, "--out", str(attention.vendor_check_path(item["key"]).with_suffix(""))]
    return cmd


def exclusion_command(kind, key, approval_ref, reason, *, remove=False):
    if remove:
        return _tool("review_exclusions") + ["remove", "--kind", kind, "--key", key, "--removed-by", approval_ref, "--reason", reason]
    return _tool("review_exclusions") + ["add", "--kind", kind, "--key", key, "--reason", reason, "--added-by", approval_ref]


def exclusion_key(item):
    """What the exclusions tool keys on for an inbox item."""
    if item["kind"] == "product":
        return "product", item["extra"]["pid"]
    if item["kind"] == "vendor":
        return "vendor", item["extra"]["vendor"].get("epos_name", "")
    if item["kind"] == "bill":
        return "bill", item["identity"]
    raise ValueError("This item can't be added to the don't-ask-again list")


def tool_command(item, action, approval_ref):
    if item["kind"] == "hold":
        raise ValueError("Hold clear uses the existing hold function")
    folder = _folder(item)
    if item["kind"] == "bill":
        return _tool("bills_sync") + ["post", "--review", str(folder / "review.csv"), "--approval-ref", approval_ref, "--expect-sha", item["extra"]["sha"], "--no-slack"]
    if item["kind"] == "product":
        return product_command(item, approval_ref)
    if item["kind"] == "deposit":
        return _tool("uf_deposits") + ["post", "--plan-dir", str(folder), "--approval-ref", approval_ref, "--expect-sha", item["extra"]["sha"], "--no-slack"]
    raise ValueError("This tool does not support manual approval or skipping. Fix the source and re-plan.")


def record_vendor_check(item, choice, approval_ref, job_id):
    """Run the read-only QuickBooks check and keep its result next to the inbox item."""
    proc = subprocess.run(vendor_command(item, approval_ref, choice, dry_run=True), capture_output=True, text=True)
    sys.stderr.write(proc.stderr[-4000:])
    try:
        result = json.loads(proc.stdout)
        if not isinstance(result, dict):
            raise ValueError
    except ValueError:
        sys.stdout.write(proc.stdout[-4000:])
        return proc.returncode or 2
    path = attention.vendor_check_path(item["key"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"requested": choice, "result": result, "checked_at": timezone.now().isoformat(),
                               "job_id": str(job_id), "exit_code": proc.returncode}, indent=1), encoding="utf-8")
    tmp.replace(path)
    print(json.dumps(result, indent=1))
    return 0 if result.get("result") in {"dry_run", "preflight_failed", "already_approved"} else (proc.returncode or 2)


def execute(action_record):
    """Called with the shared pipeline lock held. Revalidate at execution, not just enqueue."""
    data = action_record.payload
    ref = data.get("approval_ref") or f"Approved by {action_record.actor} in the portal, {action_record.created_at.astimezone(LAGOS):%Y-%m-%d %H:%M} Lagos"
    reason = action_record.reason
    if action_record.action == "daily":
        target = date.fromisoformat(data["date"])
        from code_scripts.akponora_ops.daily_run import last_closed_business_date
        # Date was bound at confirmation: never let a queued job roll to another day.
        if target > last_closed_business_date() or target < date(2026, 10, 1):
            raise ValueError("Choose a closed business day from 1 October 2026.")
        cmd = _tool("daily_run") + ["--date", target.isoformat(), "--no-slack"]
        if data["dry_run"]:
            cmd.append("--dry-run")
        if data.get("only"):
            cmd.extend(["--only", data["only"]])
        return subprocess.call(cmd)
    if action_record.action == "unexclude":
        from . import exclusions
        row = exclusions.find(data["exclusion_kind"], data["exclusion_key"])
        if row is None:
            raise ReviewChanged("This entry is no longer on the don't-ask-again list.")
        return subprocess.call(exclusion_command(row["kind"], row["key"], ref, reason, remove=True))
    if action_record.action == "exclude" and not data.get("key"):
        # A new EPOS product from the stock snapshot that no product plan has picked up yet.
        from . import products
        if data.get("exclusion_kind") != "product" or data.get("exclusion_key") not in products.unmapped_ids():
            raise ReviewChanged("This product is no longer listed as new in EPOS. Refresh the page.")
        return subprocess.call(exclusion_command("product", data["exclusion_key"], ref, reason))
    item = current_item(data["key"], data["snapshot"])
    action = action_record.action
    if action == "routine":
        if item["kind"] != "bill" or not item["extra"].get("routine"):
            raise ValueError("This action is no longer available for this item. Refresh the inbox.")
        supplier = item["extra"]["row"].get("EPOS Supplier", "")
        rc = subprocess.call(exclusion_command("routine_repeat", supplier, ref, reason or "routine repeat orders"))
        if rc != 0:
            return rc
        action = "approve"  # then post this bill exactly like Approve bill (the record keeps "routine")
    if not item.get(action):
        raise ValueError("This action is no longer available for this item. Refresh the inbox.")
    if action == "skip" and item["kind"] != "bill":
        # Deferral records a human decision only; it never advances a financial cursor.
        return 0
    if action == "exclude":
        kind, key = exclusion_key(item)
        return subprocess.call(exclusion_command(kind, key, ref, reason))
    if item["kind"] == "vendor":
        if action == "preview":
            return record_vendor_check(item, data["choice"], ref, action_record.job_id)
        check = item["extra"].get("check") or {}
        result = check.get("result") or {}
        if not (result.get("result") == "dry_run" and result.get("payload_sha256") and not result.get("problems")):
            raise ValueError("Check this supplier with QuickBooks again before approving it.")
        return subprocess.call(vendor_command(item, ref, check["requested"], dry_run=False, expect_sha=result["payload_sha256"]))
    if item["kind"] == "hold":
        from code_scripts.operations_controls import clear_posting_hold
        clear_posting_hold(approved_by=action_record.actor, reason=f"{ref}; {reason}")
        return 0
    if item["kind"] == "bill":
        folder = ops.resolve_evidence(item["run"], "bills/review.csv").parent
        path = folder / "review.csv"
        review = attention.rows(path)
        # Clear every other approval so this job authorizes exactly the selected PO.
        for row in review:
            row["Approve"] = ("yes" if action == "approve" else "skip") if row["PO"] == item["identity"] else ""
        temporary = path.with_suffix(".portal.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(review[0]))
            writer.writeheader()
            writer.writerows(review)
        temporary.replace(path)
    return subprocess.call(tool_command(item, action, ref))
