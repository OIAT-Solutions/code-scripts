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
    allowed_files = {p.parent.resolve() / p.name for p in (ops.posting_hold_path(), ops.state_root() / "mappings/company_a/vendors.csv", ops.state_root() / "mappings/company_a/approved.csv", ops.ops_root() / "uf_deposits/days.json")}
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


def make_item(kind, identity, title, reason, run=None, relative="", approve=False, skip=False, extra=None,
              exclude=False, preview=False):
    key = hashlib.sha256(f"{kind}:{identity}".encode()).hexdigest()
    item = dict(key=key, kind=kind, identity=identity, title=title, reason=reason,
                company="Akponora", date=run.business_date if run else "", approve=approve, skip=skip,
                exclude=exclude, preview=preview, run=run, relative=relative, extra=extra or {})
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


def rebind(item, paths):
    """Bind an item's confirmation to exactly these evidence files."""
    item["paths"] = list(paths)
    item["snapshot"] = snapshot(item["paths"])
    return item


# ----------------------------------------------------------------------------- products
PRODUCT_ACTIONS = {"CREATE_INVENTORY", "CREATE_NONINVENTORY", "MAPPING_ONLY"}


def mapped_product_ids():
    """EPOS ids already in the installed mapping (approved.csv). Local file only."""
    path = ops.state_root() / "mappings/company_a/approved.csv"
    return {str(r.get("EPOS Product ID", "")).strip() for r in rows(path)} - {""}


def product_reason(d, plan_by_pid):
    """One plain sentence for a catalogue decision; technical flags stay in Details."""
    target = d.get("target") or {}
    name, sku = target.get("name") or d.get("name", ""), target.get("sku", "")
    if d.get("review") == "HOLD":
        return "This product can't be set up yet. Fix it in EPOS; the next daily product check plans it again."
    if d.get("action") == "CREATE_INVENTORY":
        text = (f"Use the existing QuickBooks item {name} ({sku})." if d.get("adopt_qbo_id")
                else f"Create a new stock item in QuickBooks: {name} ({sku}). It starts at quantity zero.")
    elif d.get("action") == "CREATE_NONINVENTORY":
        text = (f"Use the existing QuickBooks item {name} ({sku})." if d.get("adopt_qbo_id")
                else f"Create a non-stock item in QuickBooks: {name} ({sku}).")
    elif d.get("action") == "MAPPING_ONLY":
        master = plan_by_pid.get(str(d.get("master_pid"))) or {}
        unit = d.get("canonical_unit") or "units"
        text = f"Sell it from the stock of {name} ({sku}): each sale uses {d.get('multiplier') or '?'} {unit}."
        if master:
            text += f" Its main product, {master.get('name', d.get('master_pid'))}, is set up in the same step."
    else:
        text = "Set up this product in QuickBooks."
    if d.get("review") == "REVIEW":
        text += " A person needs to check it first (see Details)."
    return text


def product_items(run, folder, seen, mapped, excluded, errors):
    plan = document(folder / "plan.json")
    summary = document(folder / "summary.json")
    plan_sha = summary.get("plan_sha256") or plan.get("plan_sha256", "")
    decision_shas = summary.get("decision_shas") or plan.get("decision_shas") or {}
    applied_file = folder / "applied_state.json"
    applied = set((document(applied_file).get("applied") or {}) if applied_file.exists() else {})
    decisions = [d for d in plan.get("decisions", []) if isinstance(d, dict) and d.get("pid")]
    by_pid = {str(d["pid"]): d for d in decisions}
    out = []
    for d in decisions:
        pid = str(d["pid"])
        if ("product", pid) in seen:
            continue
        seen.add(("product", pid))
        if pid in applied or pid in mapped or pid in excluded:
            continue
        if d.get("action") not in PRODUCT_ACTIONS and d.get("review") != "HOLD":
            continue
        approvable = d.get("review") in {"AUTO", "REVIEW"} and bool(decision_shas.get(pid)) and bool(plan_sha)
        reason = product_reason(d, {})
        master = None
        master_pid = str(d.get("master_pid") or "")
        target = d.get("target") or {}
        if approvable and d.get("action") == "MAPPING_ONLY" and not target.get("qbo_id") and master_pid in by_pid \
                and master_pid not in applied and master_pid not in mapped:
            master = by_pid[master_pid]
            if master.get("review") not in {"AUTO", "REVIEW"} or not decision_shas.get(master_pid) or master_pid in excluded:
                approvable = False
                reason = (f"Its main product, {master.get('name', master_pid)}, can't be set up yet, so this pack "
                          "can't be linked. Fix the main product first.")
            else:
                reason = product_reason(d, by_pid)
        extra = {"sha": plan_sha, "pid": pid, "decision_sha": decision_shas.get(pid, ""),
                 "name": d.get("name", ""), "action": d.get("action", ""), "review": d.get("review", ""),
                 "target": target, "multiplier": d.get("multiplier", ""), "flags": d.get("flags", []),
                 "reasons": d.get("reasons", []), "notes": d.get("notes", []),
                 "stock": (d.get("stock") or {}).get("units", "")}
        if master:
            extra["with_master"] = {"pid": master_pid, "name": master.get("name", ""),
                                    "decision_sha": decision_shas.get(master_pid, "")}
        item = make_item("product", pid, f"New product · {d.get('name') or pid}", reason, run, "catalogue",
                         approve=approvable, skip=True, exclude=True, extra=extra)
        # Bind to the plan itself: approving one product must not invalidate the others.
        out.append(rebind(item, [folder / "plan.json", folder / "summary.json"]))
    return out


# ----------------------------------------------------------------------------- suppliers
VENDOR_STATES = {
    "HOLD_NEAR_MATCH": "This name looks like a supplier that already exists in QuickBooks. Link it to the right one, or create it if it is really new.",
    "HOLD_GATE_OFF": "New supplier. Automatic supplier creation is off, so it needs your approval.",
    "HOLD_CAP": "New supplier. More new suppliers arrived than the daily limit allows, so it needs your approval.",
    "HOLD_NAME_TAKEN": "New supplier, but its name is already used by another QuickBooks record. Link it to the existing supplier or create it under a different name.",
}


def vendor_check_path(key):
    return ops.ops_root() / "portal_reads" / "vendor_checks" / f"{key}.json"


def vendor_check(key):
    """The latest QuickBooks check (vendors approve --dry-run) recorded for this supplier item."""
    path = vendor_check_path(key)
    if not path.exists():
        return None
    data = document(path)
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    return dict(data, result=result, path=path, ok=result.get("result") == "dry_run"
                and not result.get("problems") and bool(result.get("payload_sha256")))


def vendor_approved(vendor, mapped):
    from code_scripts.akponora_ops.review_exclusions import normalize_key
    name = normalize_key("vendor", vendor.get("epos_name") or "-")
    sid = str(vendor.get("supplier_id") or "")
    for r in mapped:
        if not (r.get("Approved By") and r.get("QBO Vendor Id")):
            continue
        if (sid and r.get("EPOS Supplier Id") == sid) or normalize_key("vendor", r.get("EPOS Supplier Name") or "-") == name:
            return True
    return False


def vendor_item(run, vendor, mapped, excluded):
    identity = str(vendor.get("supplier_id") or vendor.get("epos_name"))
    if vendor_approved(vendor, mapped) or not str(vendor.get("state", "")).startswith("HOLD"):
        return None
    from code_scripts.akponora_ops.review_exclusions import normalize_key
    if normalize_key("vendor", vendor.get("epos_name") or "-") in excluded:
        return None
    key = hashlib.sha256(f"vendor:{identity}".encode()).hexdigest()
    check = vendor_check(key)
    reason = VENDOR_STATES.get(vendor.get("state"), "This supplier needs a person to decide how it is set up in QuickBooks.")
    extra = {"vendor": vendor, "candidates": vendor_candidates(vendor),
             "check": {k: v for k, v in (check or {}).items() if k != "path"}}
    item = make_item("vendor", identity, f"Supplier · {vendor.get('epos_name', '')}", reason, run, "bills",
                     approve=bool(check and check["ok"]), skip=True, exclude=True, preview=True, extra=extra)
    if check:
        rebind(item, item["paths"] + [check["path"]])
    return item


# ----------------------------------------------------------------------------- deposits
def deposit_items(seen, errors):
    """Latest plan per day, including read-only portal re-plans."""
    from .deposits import plan_folders
    out = []
    state_file = ops.ops_root() / "uf_deposits/days.json"
    try:
        state = document(state_file).get("days", {}) if state_file.exists() else {}
    except (OSError, ValueError):
        errors.append("Deposit day records could not be read safely.")
        return out
    for path in plan_folders():
        try:
            summary = document(path)
            day = summary.get("day") or path.parent.name
            if not ops.DATE_RE.fullmatch(str(day)) or ("deposit", day) in seen:
                continue
            seen.add(("deposit", day))
            entry = state.get(day, {}) if isinstance(state, dict) else {}
            if entry.get("status") == "DEPOSITED" or any(document(p).get("complete") for p in path.parent.glob("post_*.json")):
                continue
            updated = ops._parse_dt(entry.get("updated_at"))
            held_after_plan = entry.get("status") == "HELD" and updated and updated.timestamp() > path.stat().st_mtime
            run = ops.Run(business_date=day, run_id="run_portal_plan", path=path.parent.parent, dry_run=False, status="review")
            from .deposits import reason as plain_reason
            raw = list(summary.get("reasons") or [])
            reason = plain_reason(raw[0], summary) if raw else "Till sheet and sales agree. Ready to move to the banks."
            item = make_item("deposit", day, f"Deposits · {day}", reason, run, path.parent.name,
                             approve=summary.get("status") == "READY" and not held_after_plan and bool(summary.get("payloads_sha256")),
                             skip=True, extra={"sha": summary.get("payloads_sha256", ""), "summary": summary})
            rebind(item, item["paths"] + [state_file])
            item["details_url"] = reverse("epos_qbo:company-detail", args=["company_a"]) + "?tab=deposits&day=" + day + "#deposit-details"
            out.append(item)
        except (OSError, ValueError, TypeError, KeyError, ops.EvidenceError):
            errors.append("A deposit plan could not be read safely.")
    return out


# ----------------------------------------------------------------------------- inbox
def inbox():
    items, errors, seen = [], [], set()
    try:
        safe_path(ops.posting_hold_path())
        hold = ops.posting_hold()
        if hold["active"]:
            items.append(make_item("hold", "sales", "Sales posting is paused", hold["reason"] or "Review the failed checks before clearing the hold.", approve=not bool(hold["error"])))
    except (OSError, ValueError):
        errors.append("The sales hold could not be read safely. Review the hold file before taking action.")
    items += deposit_items(seen, errors)
    from . import exclusions
    try:
        excluded = {kind: {r["key"] for r in exclusions.rows() if r["kind"] == kind} for kind in ("product", "vendor", "bill")}
    except (OSError, ValueError, csv.Error):
        excluded = {"product": set(), "vendor": set(), "bill": set()}
        errors.append("The don't-ask-again list could not be read. Excluded items may appear here.")
    try:
        mapped_products = mapped_product_ids()
        mapped_vendors = rows(ops.state_root() / "mappings/company_a/vendors.csv")
    except (OSError, ValueError, csv.Error):
        mapped_products, mapped_vendors = set(), []
        errors.append("The product or supplier lists could not be read. Review items may be out of date.")
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
                    if exclusions.normalize("bill", identity) in excluded["bill"]:
                        continue
                    items.append(make_item("bill", identity, f"Bill for PO {identity} · {row.get('EPOS Supplier', '')}",
                        row.get("Reasons") or row.get("Warnings") or "Waiting for approval. The bill will remain unpaid.", run, "bills",
                        approve=row.get("Status") == "READY" and bool(summary.get("payloads_sha256")), skip=True, exclude=True,
                        extra={"row": row, "sha": summary.get("payloads_sha256", "")}))
                for vendor in summary.get("vendor_actions", []):
                    identity = str(vendor.get("supplier_id") or vendor.get("epos_name"))
                    if ("vendor", identity) in seen:
                        continue
                    seen.add(("vendor", identity))
                    item = vendor_item(run, vendor, mapped_vendors, excluded["vendor"])
                    if item:
                        items.append(item)
            folder = run.path / "catalogue"
            if (folder / "summary.json").is_file() and (folder / "plan.json").is_file():
                items += product_items(run, folder, seen, mapped_products, excluded["product"], errors)
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
