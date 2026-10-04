"""Deposits tab for Company A: a read-only register over the deposit step's own records.

Sources (all local, under STATE_ROOT; never QuickBooks or Google from a view):
* ``ops/company_a/uf_deposits/days.json``: per-day state written by ``uf_deposits``.
* daily-run ``uf`` step folders: ``summary.json`` / ``scheduled.json`` (till-sheet report, Undeposited
  Funds balance) and one ``<day>/`` plan folder per day (``summary.json``, ``review.csv``).
* portal re-plans and status checks under ``ops/company_a/portal_reads/``.
* ``mappings/company_a/till_accounts.csv`` for bank names; ``uf_deposits/settings.env`` for tolerance.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from decimal import Decimal

from django.core.paginator import Paginator

from . import workspace_records as r

FLOOR = date(2026, 9, 25)
STEP_FOLDERS = ("uf", "uf_deposits", "deposits")
TOL_KEYS = {"OIAT_COMPANY_A_UF_TOLERANCE": "1000", "OIAT_COMPANY_A_UF_TOLERANCE_PCT": "0.5"}
LABELS = {
    "DEPOSITED": ("Banked", "success"),
    "READY": ("Ready to bank", "info"),
    "WAITING_SHEET": ("Waiting for till sheet", "neutral"),
    "NO_SALES": ("No sales posted yet", "neutral"),
    "HELD": ("Needs attention", "danger"),
}
EXPLAIN = {
    "READY": "Till sheet and sales agree. Ready to move to the banks.",
    "WAITING_SHEET": "The till sheet for this day isn't filled in yet.",
    "NO_SALES": "Sales for this day aren't in QuickBooks yet.",
}
AMOUNT = r"N(-?[\d,]+(?:\.\d+)?)"


def naira(text):
    """'N1,234.50' (the tool's format) -> '₦1,234.50'."""
    n = r.number(str(text).strip().lstrip("N₦"))
    return r.money(n) if n is not None else str(text)


def _amount(match, group):
    return naira(match[group])


# (pattern, sentence builder). Patterns follow the exact strings in code_scripts/akponora_ops/uf_deposits.py
# and till_sheet.py; anything else falls back to a generic sentence (raw text stays in Details).
REASONS = [
    (re.compile(r"^post stopped: (.*)", re.I | re.S),
     lambda m: "Banking this day stopped part-way. Nothing will be repeated; open details before retrying."),
    (re.compile(rf"sheet total {AMOUNT} vs receipts {AMOUNT}: difference {AMOUNT} is over the tolerance {AMOUNT}"),
     lambda m: f"Till sheet total ({_amount(m, 1)}) and sales ({_amount(m, 2)}) differ by {naira(str(abs(r.number(m[3]))))}, "
               f"which is more than the allowed {_amount(m, 4)}. Check the sheet or the sales for this day."),
    (re.compile(r"till line '([^']*)' = [^:]*: (?:unknown till line|TID \S+ not in till_accounts|TID \S+ is marked Active=no|no active cash row)"),
     lambda m: f"The till sheet has a line we don't recognise ({m[1]}). Add it to the till accounts list."),
    (re.compile(r"till line '([^']*)' is negative"),
     lambda m: f"The till sheet shows a negative amount for {m[1]}. Correct the sheet."),
    (re.compile(r"is inside the QBO closed period"),
     lambda m: "This day is in a closed period in QuickBooks. Ask the administrator."),
    (re.compile(r"QBO account (?:Id )?(\S+)(?: \(([^)]*)\))? (?:not found|is inactive|is .+, not Bank|has number)"),
     lambda m: f"A bank account for this day isn't set up correctly in QuickBooks ({m[2] or m[1]})."),
    (re.compile(r"go to a bank that is not in till_accounts"),
     lambda m: "Some of this day's sales were banked by hand into an account the till accounts list doesn't know. Check before banking the rest."),
    (re.compile(r"already deposited outside this tool"),
     lambda m: "Some of this day's sales were already banked by hand. Check before banking the rest."),
    (re.compile(rf"is over the automatic cap {AMOUNT}"),
     lambda m: f"This day is larger than the automatic limit ({_amount(m, 1)}). Approve it yourself if it's correct."),
    (re.compile(r"no SalesReceipts in QBO"), lambda m: EXPLAIN["NO_SALES"]),
    (re.compile(r"SYSTEM \(EPOS total\) box is blank"),
     lambda m: "The till sheet's SYSTEM total is blank, so the day isn't finished on the sheet."),
    (re.compile(r"^(.+?) box is blank"),
     lambda m: "The till sales breakdown for this day isn't complete yet."),
    (re.compile(r"sheet day is blank"), lambda m: "The till sheet for this day is empty."),
    (re.compile(r"has \d+ blocks for"), lambda m: "This day appears twice on the till sheet. Remove the duplicate."),
    (re.compile(r"has no (?:tab|block for)"), lambda m: EXPLAIN["WAITING_SHEET"]),
    (re.compile(r"CASH box\(es\)"), lambda m: "The cash boxes for this day don't follow the usual layout. Check the sheet."),
    (re.compile(r"already deposited by uf_deposits"), lambda m: "Already banked."),
]


def reason(text, summary=None):
    text = str(text or "").strip()
    for pattern, build in REASONS:
        match = pattern.search(text)
        if match:
            try:
                return build(match)
            except (TypeError, ValueError, AttributeError):
                break
    return "This day needs a check. Open details for the recorded reason."


# ----------------------------------------------------------------------------- readers
def settings():
    """Tolerance exactly as the step reads it: environment, then settings.env overrides."""
    values = {k: os.environ.get(k, default) for k, default in TOL_KEYS.items()}
    values.update(settings_overrides())
    return {k: r.number(v) for k, v in values.items()}


def settings_path():
    return r.ops.ops_root() / "uf_deposits" / "settings.env"


def settings_overrides():
    path = settings_path()
    out = {}
    if not path.exists():
        return out
    for line in r.safe(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (x.strip() for x in line.split("=", 1))
        value = value.split(" #", 1)[0].strip().strip("'\"")
        if key in TOL_KEYS and value:
            out[key] = value
    return out


def plan_folders():
    """Day plan summaries, newest first: daily-run step folders, then portal re-plans."""
    candidates = []
    for run in r.ops.list_runs(limit=200):
        if run.dry_run:
            continue
        for name in STEP_FOLDERS:
            folder = run.path / name
            if folder.is_dir():
                candidates += [p for p in folder.glob("*/summary.json") if r.ops.DATE_RE.fullmatch(p.parent.name)]
    root = r.read_root()
    if root.exists():
        candidates += sorted(root.glob("*/deposits/*/summary.json"))[-200:]
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def till_accounts():
    path = r.ops.state_root() / "mappings/company_a/till_accounts.csv"
    return r.rows(path) if path.exists() else []


def bank_names(accounts):
    names = {}
    for row in accounts:
        label = row.get("Till sheet line") or row.get("QBO account number") or "Bank"
        tid = row.get("Terminal / TID")
        text = f"{label} ({tid})" if tid else label
        for key in (row.get("QBO account number"), row.get("QBO account Id")):
            if key:
                names.setdefault(key, [])
                if text not in names[key]:
                    names[key].append(text)
    return names


def step_reports(errors):
    """Till-sheet report and Undeposited Funds balance from the newest records that have them."""
    reports, balance, checked = [], None, None
    for run in r.ops.list_runs(limit=50):
        if run.dry_run:
            continue
        for name in STEP_FOLDERS:
            for file in ("scheduled.json", "summary.json"):
                path = run.path / name / file
                if not path.exists():
                    continue
                try:
                    doc = r.document(path)
                except (OSError, ValueError):
                    errors.append("Some deposit records could not be read.")
                    continue
                reports.append((path.stat().st_mtime, doc))
                value = r.number(str(doc.get("uf_balance") or "").lstrip("N₦"))
                if balance is None and value is not None:
                    balance, checked = value, run.finished_at or r.timestamp(doc.get("finished_at"))
        step = next((s for s in run.steps if s.name in STEP_FOLDERS), None)
        value = r.number(str((step.counts if step else {}).get("uf_balance") or "").lstrip("N₦"))
        if balance is None and value is not None:
            balance, checked = value, run.finished_at
    status_path = r.read_root() / "latest_deposit_status.json"
    if status_path.exists():
        try:
            reports.append((status_path.stat().st_mtime, r.document(status_path)))
        except (OSError, ValueError):
            errors.append("The latest till-sheet check could not be read.")
    report = {}
    for _, doc in sorted(reports, key=lambda x: x[0], reverse=True):
        if isinstance(doc.get("till_sheet"), dict) and not doc["till_sheet"].get("error"):
            report = doc["till_sheet"]
            break
    return report, balance, checked


def _completed(folder):
    try:
        return any(r.document(p).get("complete") for p in folder.glob("post_*.json"))
    except (OSError, ValueError):
        return False


def _stopped(folder):
    try:
        return next((r.document(p).get("stopped") for p in sorted(folder.glob("post_*.json"), reverse=True) if r.document(p).get("stopped")), "")
    except (OSError, ValueError):
        return ""


def day_rows(state, plans, report, tol, end):
    missing = set(report.get("missing") or [])
    incomplete = {x.get("day"): x.get("reason", "") for x in report.get("incomplete") or [] if isinstance(x, dict)}
    rows, day = [], FLOOR
    while day <= end and len(rows) < 1000:
        key = day.isoformat()
        entry = state.get(key) or {}
        doc, folder = plans.get(key, ({}, None))
        status = entry.get("status") or ""
        updated = r.timestamp(entry.get("updated_at"))
        if status != "DEPOSITED" and folder and doc.get("state") and (not updated or (folder / "summary.json").stat().st_mtime > updated.timestamp()):
            status = doc["state"]  # a newer plan than the recorded state
        if folder and _completed(folder):
            status = "DEPOSITED"
        if not status and (key in missing or key in incomplete):
            status = "WAITING_SHEET"
        raw = list(entry.get("reasons") or ([entry["reason"]] if entry.get("reason") else []))
        if folder and doc.get("state") == status and doc.get("reasons"):
            raw = list(doc["reasons"])
        stopped = _stopped(folder) if folder else ""
        if stopped and status != "DEPOSITED":
            raw = [f"post stopped: {stopped}"] + raw
        if status == "WAITING_SHEET" and not raw and incomplete.get(key):
            raw = [incomplete[key]]
        label, tone = LABELS.get(status, ("Not checked yet", "neutral"))
        if status == "DEPOSITED":
            message = f"Moved to the banks on {updated:%-d %b %Y}." if updated else "Moved to the banks."
        elif status == "HELD":
            message = reason(raw[0]) if raw else "This day needs a check. Open details."
        elif status == "WAITING_SHEET" and raw:
            message = reason(raw[0])
        else:
            message = EXPLAIN.get(status, "Not checked yet.")
        sales = r.number(doc.get("receipts_total") if doc.get("receipts_total") is not None else entry.get("receipts_total"))
        sheet = r.number(doc.get("sheet_total"))
        diff = sheet - sales if sheet is not None and sales is not None else None
        abs_tol, pct = tol.get("OIAT_COMPANY_A_UF_TOLERANCE"), tol.get("OIAT_COMPANY_A_UF_TOLERANCE_PCT")
        allowed = max(abs_tol, sales * pct / Decimal(100)) if None not in (abs_tol, pct, sales) else None
        sheet_text = r.money(sheet) if sheet is not None else ("Incomplete" if key in incomplete else "Not entered" if key in missing or status == "WAITING_SHEET" else "Not checked yet")
        rows.append(dict(day=day, date=key, status=status, label=label, tone=tone, message=message, raw=raw,
                         sales=r.money(sales) if sales is not None else "Not checked yet", sheet=sheet_text,
                         difference=r.money(diff) if diff is not None else "", outside=bool(diff is not None and allowed is not None and abs(diff) > allowed),
                         allowed=r.money(allowed) if allowed is not None else "", summary=doc, folder=folder, updated=updated,
                         url=f"?tab=deposits&day={key}#deposit-details"))
        day += timedelta(days=1)
    rows.reverse()
    return rows


def bank_lines(folder, names, label):
    out, transfer = [], Decimal(0)
    for bank in r.rows(folder / "review.csv") if (folder / "review.csv").exists() else []:
        number, acct = bank.get("Bank No", ""), bank.get("QBO Account Id", "")
        name = " / ".join(names.get(number) or names.get(acct) or [number or acct or "Bank"])
        transfer += r.number(bank.get("Transfer Out")) or Decimal(0)
        out.append(dict(name=name, kind=bank.get("Kind", ""), sheet=r.money(bank.get("Sheet Amount")), target=r.money(bank.get("Target")),
                        final=r.money(bank.get("Final")), status=bank.get("Status") or label))
    return out, transfer


def latest_job():
    from ..models import RunJob
    return RunJob.objects.filter(scope=RunJob.SCOPE_WORKSPACE_READ, company_key="company_a",
                                 inventory_options_json__action__in=["deposit_plan", "deposit_status"]).order_by("-created_at").first()


def sheet_url():
    sheet_id = os.getenv("OIAT_COMPANY_A_TILL_SHEET_ID", "").strip()
    if not sheet_id:
        from code_scripts.akponora_ops.till_sheet import DEFAULT_SHEET_ID
        sheet_id = DEFAULT_SHEET_ID
    return "https://docs.google.com/spreadsheets/d/" + sheet_id if re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id or "") else ""


def context(request):
    errors = []
    state_path = r.ops.ops_root() / "uf_deposits/days.json"
    try:
        state = r.document(state_path).get("days", {}) if state_path.exists() else {}
        if not isinstance(state, dict):
            raise ValueError("Invalid days")
    except (OSError, ValueError, TypeError):
        state = {}
        errors.append("The deposit day records could not be read.")
    report, balance, checked = step_reports(errors)
    plans = {}
    for path in plan_folders():
        try:
            doc = r.document(path)
            day = doc.get("day") or path.parent.name
            if r.ops.DATE_RE.fullmatch(str(day)) and day not in plans:
                plans[day] = (doc, path.parent)
        except (OSError, ValueError, TypeError):
            errors.append("Some deposit plans could not be read.")
    try:
        tol = settings()
    except (OSError, ValueError):
        tol = {}
        errors.append("The deposit tolerance could not be read.")
    from code_scripts.akponora_ops.daily_run import last_closed_business_date
    has_records = bool(state or plans or report)
    rows = day_rows(state, plans, report, tol, last_closed_business_date()) if has_records else []
    try:
        accounts = till_accounts()
    except (OSError, ValueError):
        accounts = []
        errors.append("The till accounts list could not be read.")
    from . import attention
    items, _ = attention.inbox()
    decisions = {i["identity"]: i for i in items if i["kind"] == "deposit"}
    for row in rows:
        row["decision"] = decisions.get(row["date"])
        row["approve"] = bool(row["status"] == "READY" and row["decision"] and row["decision"]["approve"])
    selected = next((row for row in rows if row["date"] == request.GET.get("day")), None)
    banks, transfer = [], Decimal(0)
    if selected and selected["folder"]:
        try:
            banks, transfer = bank_lines(selected["folder"], bank_names(accounts), selected["label"])
        except (OSError, ValueError):
            errors.append("The bank breakdown for this day could not be read.")
    receipts = (selected or {}).get("summary", {}).get("receipts") if selected else None
    missing = sorted(report.get("missing") or [])
    return dict(
        deposit_page=Paginator(rows, 20).get_page(request.GET.get("page")), deposit_has_records=has_records,
        deposit_selected=selected, deposit_banks=banks, deposit_transfer=r.money(transfer) if transfer else "",
        deposit_receipts=[(k, v) for k, v in receipts.items() if isinstance(v, (int, float))] if isinstance(receipts, dict) else [],
        deposit_errors=list(dict.fromkeys(errors)), deposit_balance=r.money(balance) if balance is not None else "",
        deposit_checked=checked, deposit_last_day=date.fromisoformat(report["last_complete_day"]) if r.ops.DATE_RE.fullmatch(str(report.get("last_complete_day") or "")) else None,
        deposit_missing=[date.fromisoformat(d) for d in missing if r.ops.DATE_RE.fullmatch(str(d))],
        deposit_incomplete=[x.get("day") for x in report.get("incomplete") or [] if isinstance(x, dict)],
        deposit_report_text=report.get("text", ""), deposit_waiting=sum(row["status"] in {"READY", "HELD"} for row in rows),
        deposit_sheet_url=sheet_url(), deposit_enabled=os.getenv(r.ops.UF_ENV, "").strip().lower() in {"1", "true", "yes", "on"},
        deposit_job=latest_job(), deposit_tolerance=tol, deposit_tolerance_overrides=settings_overrides() if settings_path().exists() else {},
        deposit_accounts=[dict(line=a.get("Till sheet line", ""), tid=a.get("Terminal / TID", ""), number=a.get("QBO account number", ""),
                               kind=a.get("Kind (cash/card/transfer)") or a.get("Kind", ""), active=a.get("Active", "")) for a in accounts])


def save_tolerance(values, *, actor, reason):
    """Write the tolerance keys to settings.env (other lines kept) and audit each change.

    The deposit step re-reads this file at the start of every run; no restart is needed."""
    from django.db import transaction
    from ..models import PortalSettingChange

    clean = {}
    for key, text in values.items():
        if key not in TOL_KEYS:
            raise ValueError("Unknown setting")
        n = r.number(text)
        if n is None or n < 0 or (key.endswith("_PCT") and n > 100):
            raise ValueError("Enter a number of zero or more (the percentage at most 100).")
        clean[key] = format(n.normalize(), "f")
    current = settings()
    path = settings_path()
    lines = r.safe(path).read_text(encoding="utf-8").splitlines() if path.exists() else [
        "# Deposit tolerance, edited from the OIAT portal. Read by uf_deposits at the start of every run."]
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in clean]
    kept += [f"{key}={value}" for key, value in clean.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    with transaction.atomic():
        changed = []
        for key, value in clean.items():
            old = current.get(key)
            if old is not None and r.number(value) == old:
                continue
            changed.append(PortalSettingChange.objects.create(company_key="company_a", setting=key,
                old_value="" if old is None else format(old.normalize(), "f"), new_value=value, reason=reason, actor=actor))
        if changed:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
            tmp.replace(path)
    return changed
