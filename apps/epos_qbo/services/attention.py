"""Evidence-backed inbox. Reading this module never contacts QBO or changes evidence."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.utils import timezone

from . import company_a_ops as ops
from ..models import CompanyConfigRecord, RunJob, PortalReviewAction


def safe_path(path):
    path = Path(path)
    resolved = path.resolve()
    allowed_files = {p.parent.resolve() / p.name for p in (ops.posting_hold_path(), ops.state_root() / "mappings/company_a/vendors.csv", ops.ops_root() / "uf_deposits/days.json")}
    if not resolved.is_relative_to(ops.state_root().resolve()) or (not resolved.is_relative_to(ops.daily_root().resolve()) and not resolved.is_relative_to((ops.ops_root()/"portal_reads").resolve()) and resolved not in allowed_files):
        raise ValueError("Evidence points outside the state directory")
    if path.is_file() and path.stat().st_size > ops.MAX_VIEW_BYTES:
        raise ValueError("Evidence file is too large")
    return path


def vendor_candidates(vendor):
    out = []
    for candidate in vendor.get("candidates", []):
        match = re.fullmatch(r"(\d+) (.+) \((?:[\d.]+|September bills: \d+)\)", str(candidate))
        if match:
            out.append({"id": match[1], "name": match[2]})
    return out


def rows(path):
    path = safe_path(path)
    if not path.exists():
        return []
    if path.stat().st_size > ops.MAX_VIEW_BYTES:
        raise ValueError("Review file is too large; open the daily run evidence.")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def document(path):
    path = safe_path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected an evidence object")
    return data


def snapshot(paths):
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        path = safe_path(path)
        # A changed/missing companion file invalidates the confirmation, too.
        digest.update(str(path).encode())
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
    return digest.hexdigest()


def make_item(kind, identity, title, reason, run=None, relative="", approve=False, skip=False, extra=None):
    key = hashlib.sha256(f"{kind}:{identity}".encode()).hexdigest()
    item = dict(key=key, kind=kind, identity=identity, title=title, reason=reason,
                company="Akponora", date=run.business_date if run else "", approve=approve, skip=skip,
                run=run, relative=relative, extra=extra or {})
    if run:
        folder = ops.resolve_evidence(run, relative + "/summary.json").parent if relative else run.path
        paths = [folder / n for n in ("summary.json", "review.csv", "review_lines.csv", "plan.json", "payloads.jsonl", "results.csv", "apply_receipt.json", "post_summary.json")]
        paths += list(folder.glob("post_*.json"))
        item["paths"] = paths
        item["details_url"] = reverse("epos_qbo:company-a-run-detail", args=[run.business_date, run.run_id])
    else:
        item["paths"] = [ops.posting_hold_path()]
        item["details_url"] = reverse("epos_qbo:company-a-runs")
    if kind == "vendor":
        item["paths"].append(ops.state_root() / "mappings/company_a/vendors.csv")
    item["snapshot"] = snapshot(item["paths"])
    return item


def inbox():
    items, errors, seen = [], [], set()
    try:
        safe_path(ops.posting_hold_path())
        hold = ops.posting_hold()
        if hold["active"]:
            items.append(make_item("hold", "sales", "Sales posting is paused", hold["reason"] or "Review the failed checks before clearing the hold.", approve=not bool(hold["error"])))
    except (OSError, ValueError):
        errors.append("The sales hold could not be read safely. Review the hold file before taking action.")
    # Latest deposit plans include read-only portal replans as well as daily plans.
    from .deposits import plan_folders
    for path in plan_folders():
        try:
            summary = document(path)
            day = summary.get("day") or path.parent.name
            if not ops.DATE_RE.fullmatch(day) or ("deposit",day) in seen:
                continue
            seen.add(("deposit",day))
            state_file=ops.ops_root()/"uf_deposits/days.json"
            state=document(state_file).get("days",{}).get(day,{}) if state_file.exists() else {}
            if state.get("status")=="DEPOSITED" or any(document(p).get("complete") for p in path.parent.glob("post_*.json")):
                continue
            held_after_plan = state.get("status")=="HELD" and ops._parse_dt(state.get("updated_at")) and ops._parse_dt(state["updated_at"]).timestamp()>path.stat().st_mtime
            run=ops.Run(business_date=day,run_id="run_portal_plan",path=path.parent.parent,dry_run=False,status="review")
            item=make_item("deposit",day,f"Deposits · {day}","; ".join(summary.get("reasons") or []) or "Review the till sheet split before banking this day.",run,path.parent.name,
                approve=summary.get("status")=="READY" and not held_after_plan and bool(summary.get("payloads_sha256")),skip=True,
                extra={"sha":summary.get("payloads_sha256",""),"summary":summary})
            item["paths"].append(state_file);item["snapshot"]=snapshot(item["paths"])
            item["details_url"]=reverse("epos_qbo:company-detail",args=["company_a"])+"?tab=deposits&day="+day+"#deposit-details"
            items.append(item)
        except (OSError,ValueError,TypeError,KeyError,ops.EvidenceError):
            errors.append("A deposit plan could not be read safely.")
    runs = ops.list_runs()
    done_bills = set()
    for run in runs:
        if run.dry_run:
            continue
        try:
            done_bills.update(r.get("PO") for r in rows(run.path / "bills/results.csv") if r.get("status") in {"POSTED", "ADOPTED", "RESOLVED"})
        except (OSError, ValueError, csv.Error):
            errors.append(f"Bill results for {run.business_date} could not be read; review the daily run.")
    for run in runs:
        if run.dry_run:
            continue
        try:
            # Each identity is shown from its newest plan, even when resolved.
            folder = run.path / "bills"
            if (folder / "summary.json").is_file():
                summary = document(folder / "summary.json")
                done = {r.get("PO") for r in rows(folder / "results.csv") if r.get("status") in {"POSTED", "ADOPTED", "RESOLVED"}}
                for row in rows(folder / "review.csv"):
                    identity = row.get("PO", "")
                    marker = ("bill", identity)
                    if not identity or marker in seen:
                        continue
                    seen.add(marker)
                    if identity in done_bills or identity in done or row.get("Status") not in {"READY", "HOLD"}:
                        continue
                    items.append(make_item("bill", identity, f"Bill for PO {identity} · {row.get('EPOS Supplier', '')}",
                        row.get("Reasons") or row.get("Warnings") or "Waiting for approval. The bill will remain unpaid.", run, "bills",
                        approve=row.get("Status") == "READY" and bool(summary.get("payloads_sha256")), skip=True,
                        extra={"row": row, "sha": summary.get("payloads_sha256", "")}))
                for vendor in summary.get("vendor_actions", []):
                    identity = str(vendor.get("supplier_id") or vendor.get("epos_name"))
                    marker = ("vendor", identity)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    mapped = rows(ops.state_root() / "mappings/company_a/vendors.csv")
                    if any(r.get("Approved By") and r.get("QBO Vendor Id") and ((vendor.get("supplier_id") and r.get("EPOS Supplier Id") == str(vendor.get("supplier_id"))) or r.get("EPOS Supplier Name") == vendor.get("epos_name")) for r in mapped):
                        continue
                    if not str(vendor.get("state", "")).startswith("HOLD"):
                        continue
                    items.append(make_item("vendor", identity, f"Supplier · {vendor.get('epos_name', '')}",
                        vendor.get("detail") or "Choose the existing supplier after checking the possible matches.", run, "bills",
                        approve=bool(vendor_candidates(vendor)), skip=True, extra={"vendor": vendor, "candidates": vendor_candidates(vendor)}))
            folder = run.path / "catalogue"
            if (folder / "summary.json").is_file() and (folder / "plan.json").is_file():
                plan = document(folder / "plan.json")
                ids = sorted(str(d["pid"]) for d in plan.get("decisions", []) if d.get("review") in {"AUTO", "REVIEW", "HOLD"})
                fresh = [pid for pid in ids if ("product", pid) not in seen]
                seen.update(("product", pid) for pid in ids)
                installed = (folder / "apply_receipt.json").exists() and bool(document(folder / "apply_receipt.json").get("installed"))
                if fresh and (not installed or any(d.get("review") == "HOLD" for d in plan.get("decisions", []))):
                    eligible = [d for d in plan.get("decisions", []) if d.get("review") in {"AUTO", "REVIEW"}]
                    items.append(make_item("catalogue", ",".join(ids), f"Products · {len(ids)} to review", 
                        f"Approve applies all {len(eligible)} eligible products in this plan at quantity zero and installs their mapping. Held products need an EPOS fix.", run, "catalogue",
                        approve=not installed and bool(eligible) and bool(plan.get("plan_sha256")), skip=True, extra={"sha": plan.get("plan_sha256", ""), "decisions": plan.get("decisions", [])}))
            for name in ("uf", "uf_deposits", "deposits"):
                folder = run.path / name
                if not folder.is_dir():
                    continue
                for day in sorted(folder.iterdir()):
                    if not day.is_dir() or not ops.DATE_RE.fullmatch(day.name) or not (day / "summary.json").exists():
                        continue
                    state_file = ops.ops_root() / "uf_deposits/days.json"
                    if state_file.exists() and document(state_file).get("days", {}).get(day.name, {}).get("status") == "DEPOSITED":
                        continue
                    marker = ("deposit", day.name)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    summary = document(day / "summary.json")
                    if any(document(p).get("complete") for p in day.glob("post_*.json")):
                        continue
                    items.append(make_item("deposit", day.name, f"Deposits · {day.name}", 
                        "; ".join(summary.get("reasons") or []) or "Review the till sheet split before depositing this day's receipts.", run, f"{name}/{day.name}",
                        approve=summary.get("status") == "READY" and bool(summary.get("payloads_sha256")), skip=True,
                        extra={"sha": summary.get("payloads_sha256", ""), "summary": summary}))
        except (OSError, ValueError, KeyError, TypeError, csv.Error, ops.EvidenceError) as exc:
            errors.append(f"Evidence for {run.business_date} could not be read. Open the daily run details. ({type(exc).__name__})")
    deferred = {(r.payload.get("key"), r.payload.get("snapshot")) for r in PortalReviewAction.objects.filter(action="skip", job__status=RunJob.STATUS_SUCCEEDED, finished_at__isnull=False)}
    return [i for i in items if (i["key"], i["snapshot"]) not in deferred], errors


def blockers(now=None):
    """Operational failures are red regardless of the old dashboard warning classification."""
    now = now or timezone.now()
    expected = now.astimezone(ZoneInfo("Africa/Lagos")).date() - timedelta(days=1)
    out = []
    for company in CompanyConfigRecord.objects.filter(is_active=True):
        if company.company_key == "company_a":
            hold = ops.posting_hold()
            runs = [r for r in ops.list_runs() if not r.dry_run]
            successful = [date.fromisoformat(r.business_date) for r in runs if ops.DATE_RE.fullmatch(r.business_date) and r.step("sales") and r.step("sales").status == "ok" and r.step("sales").counts.get("mode") == "post" and r.step("sales").counts.get("reconcile_status") == "MATCH"]
            latest = max(successful, default=None)
            reason = "Sales posting is paused. Review the hold and fix its cause." if hold["active"] else ""
            missing = [date(2026, 10, 1) + timedelta(days=i) for i in range(max(0, min((expected-date(2026, 10, 1)).days+1, 366))) if date(2026, 10, 1) + timedelta(days=i) not in successful]
            if not reason and (not latest or latest < expected or missing):
                reason = f"Sales are behind. Last verified sales day: {latest or 'not available'}. Review the daily runs and run missing days oldest first."
        else:
            jobs = RunJob.objects.filter(company_key=company.company_key, scope=RunJob.SCOPE_SINGLE).order_by("-created_at")
            job = jobs.first()
            # Successful artifact evidence is needed: a successful subprocess may merely skip sales.
            from ..models import RunArtifact
            artifact = RunArtifact.objects.filter(company_key=company.company_key, kind=RunArtifact.KIND_SALES_UPLOAD, reconcile_status="MATCH").order_by("-target_date").first()
            latest = artifact.target_date if artifact else None
            reason = ""
            if job and job.status == RunJob.STATUS_FAILED:
                reason = "Sales did not post. Open the failed run, fix the cause, then retry the day."
            elif job and job.status == RunJob.STATUS_RUNNING and job.started_at and now - job.started_at > timedelta(hours=2):
                reason = "The sales run has been stuck for over two hours. Open its details and investigate before retrying."
            elif not latest or latest < expected:
                reason = f"Sales are behind. Last recorded sales day: {latest or 'not available'}. Review missing days and the schedule."
        if reason:
            out.append({"company": company.display_name, "reason": reason, "url": reverse("epos_qbo:company-a-runs") if company.company_key == "company_a" else reverse("epos_qbo:runs")})
    return out
