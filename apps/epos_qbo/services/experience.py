"""Read-only, business-facing summaries shared by Home and Daily runs."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.db.models import Q
from django.utils import timezone

from ..models import CompanyConfigRecord, RunArtifact, RunJob, RunSchedule
from ..business_date import get_target_trading_date
from . import company_a_ops as ops
from .attention import inbox


def money(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def money_label(value):
    return f"₦{value:,.2f}" if value is not None else "Not recorded"


def verified_sales(run):
    sales = run.step("sales")
    return bool(not run.dry_run and sales and sales.status == "ok"
                and sales.counts.get("mode") == "post"
                and sales.counts.get("reconcile_status") == "MATCH")


def date_value(value):
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def run_message(run):
    if run.status in {"unreadable", "incomplete", "unknown"}:
        return "Outcome not available", "The run record is incomplete or could not be read.", "neutral"
    if run.dry_run:
        if run.status == "failed":
            return "Preview could not finish", "Nothing was posted by this preview. Open the details.", "danger"
        return "Preview ready" if run.status == "ok" else "Preview needs review", "Nothing was posted by this preview.", "neutral"
    if verified_sales(run):
        sales = run.step("sales")
        amount = money_label(money(sales.counts.get("qbo_total")))
        message = f"Sales confirmed in QuickBooks: {amount}."
        bills = run.step("bills")
        if bills and "posted" in bills.counts:
            message += f" {bills.counts['posted']} bills posted in this attempt."
        if run.status == "failed":
            return "Sales confirmed · check other steps", message, "danger"
        return "Sales confirmed" if run.status == "ok" else "Needs review", message, "success" if run.status == "ok" else "warning"
    if run.status == "failed":
        return "Could not finish", "Sales posting has not been confirmed for this attempt. Open the details before trying again.", "danger"
    return "Needs review", "Sales posting has not been confirmed for this attempt. Check the daily steps.", "warning"


def job_message(job, confirmed=False):
    if job.status == RunJob.STATUS_FAILED:
        return "Latest attempt failed", "Open the details before trying again.", "danger"
    if job.status == RunJob.STATUS_RUNNING:
        if job.started_at and timezone.now() - job.started_at > timedelta(hours=2):
            return "Needs attention", "This run has been running for over two hours. Check it before starting another.", "danger"
        return "In progress", "This attempt is still running.", "neutral"
    if job.status == RunJob.STATUS_QUEUED:
        return "Waiting to start", "This attempt has not started yet.", "neutral"
    if job.status == RunJob.STATUS_CANCELLED:
        return "Cancelled", "This attempt was cancelled.", "neutral"
    if confirmed:
        return "Sales confirmed", "Sales totals match QuickBooks.", "success"
    return "Finished · check outcome", "The run finished, but sales posting has not been confirmed.", "warning"


def confirmed_artifact(artifact):
    return (artifact.kind == RunArtifact.KIND_SALES_UPLOAD and artifact.reconcile_status == "MATCH"
            and isinstance(artifact.upload_stats_json, dict) and not artifact.upload_stats_json.get("dry_run"))


def daily_rows(company_key="", start=None, end=None, include_previews=True):
    """Bound recent history; never fabricate a business date from a job creation date."""
    companies = {c.company_key: c.display_name for c in CompanyConfigRecord.objects.all()}
    groups = {}

    def add(key, day, attempt):
        if company_key and key != company_key:
            return
        if start and (not day or day < start) or end and (not day or day > end):
            return
        # Undated jobs must remain distinct rather than merge unrelated work.
        identity = (key, day, None if day else attempt["url"])
        row = groups.setdefault(identity, dict(company_key=key, company=companies.get(key, key or "Several companies"),
            day=day, attempts=[], amount=None, confirmed=False))
        row["attempts"].append(attempt)

    for run in ops.list_runs():
        if run.dry_run and not include_previews:
            continue
        day = date_value(run.business_date)
        if day is None:
            continue
        label, message, tone = run_message(run)
        add("company_a", day, dict(url=reverse("epos_qbo:company-a-run-detail", args=[run.business_date, run.run_id]),
            label=label, message=message, tone=tone, when=run.finished_at, order=run.run_id.replace("_dry", ""),
            confirmed=verified_sales(run), amount=money(run.step("sales").counts.get("qbo_total")) if verified_sales(run) else None,
            preview=run.dry_run, state={"ok":"succeeded", "review":"review", "failed":"failed"}.get(run.status,"unknown")))

    artifact_query = RunArtifact.objects.filter(kind=RunArtifact.KIND_SALES_UPLOAD).filter(~Q(company_key="company_a") | Q(target_date__lt=date(2026, 10, 1)))
    if company_key:
        artifact_query = artifact_query.filter(company_key=company_key)
    if start:
        artifact_query = artifact_query.filter(target_date__gte=start)
    if end:
        artifact_query = artifact_query.filter(target_date__lte=end)
    artifacts = list(artifact_query.order_by("-target_date", "-processed_at", "-id")[:1000])
    by_job = defaultdict(list)
    for artifact in artifacts:
        if artifact.run_job_id:
            by_job[artifact.run_job_id].append(artifact)
    job_query = RunJob.objects.exclude(scope__in=[RunJob.SCOPE_PORTAL_REVIEW, RunJob.SCOPE_INVENTORY_PIPELINE, RunJob.SCOPE_INVENTORY_SYNC])
    if company_key:
        job_query = job_query.filter(Q(company_key=company_key) | Q(id__in=by_job))
    if start:
        job_query = job_query.filter(Q(target_date__gte=start) | Q(id__in=by_job))
    if end:
        job_query = job_query.filter(Q(target_date__lte=end) | Q(id__in=by_job))
    jobs = list(job_query.order_by("-created_at")[:200])
    job_ids = {j.id for j in jobs}
    for job in jobs:
        records = by_job[job.id]
        if not records and job.company_key == "company_a" and (not job.target_date or job.target_date >= date(2026, 10, 1)):
            continue  # Company A outcomes come from its daily evidence, not the old pipeline.
        identities = {(a.company_key, a.target_date) for a in records} or {(job.company_key or "", job.target_date)}
        for key, day in identities:
            if key == "company_a" and (not day or day >= date(2026, 10, 1)):
                continue
            matches = [a for a in records if a.company_key == key and a.target_date == day and confirmed_artifact(a)]
            chosen = matches[0] if matches else None
            label, message, tone = job_message(job, bool(chosen))
            add(key, day, dict(url=reverse("epos_qbo:run-detail", args=[job.id]), label=label, message=message, tone=tone,
                when=job.created_at, order=str(job.created_at), confirmed=bool(chosen), amount=money(chosen.reconcile_qbo_total) if chosen else None, preview=False, state=job.status))
    for artifact in artifacts:
        if artifact.run_job_id in job_ids:
            continue
        confirmed = confirmed_artifact(artifact)
        url = reverse("epos_qbo:run-detail", args=[artifact.run_job_id]) if artifact.run_job_id else reverse("epos_qbo:company-detail", args=[artifact.company_key]) if artifact.company_key in companies else reverse("epos_qbo:companies-list")
        add(artifact.company_key, artifact.target_date, dict(url=url,
            label="Sales confirmed" if confirmed else "Needs review", message="Sales totals match QuickBooks." if confirmed else "Sales posting has not been confirmed.",
            tone="success" if confirmed else "warning", when=artifact.processed_at, order=str(artifact.processed_at or ""),
            confirmed=confirmed, amount=money(artifact.reconcile_qbo_total) if confirmed else None, preview=False, state="succeeded" if confirmed else "review"))
    for row in groups.values():
        row["attempts"].sort(key=lambda a: (a["when"].isoformat() if a["when"] else "", a["order"]), reverse=True)
        confirmed = next((a for a in row["attempts"] if a["confirmed"]), None)
        row.update(confirmed=bool(confirmed), amount=confirmed["amount"] if confirmed else None)
        row["amount_label"] = money_label(row["amount"])
        row["latest"] = row["attempts"][0]
        row["label"] = "Sales confirmed" if confirmed else row["latest"]["label"]
        row["tone"] = "success" if confirmed else row["latest"]["tone"]
        row["latest_issue"] = bool(confirmed and row["latest"]["tone"] in {"danger", "warning"})
        if row["latest_issue"]:
            row["label"], row["tone"] = "Sales confirmed · needs attention", "warning"
    return sorted(groups.values(), key=lambda r: (r["day"] or date.min, r["company"]), reverse=True)


def other_activity(company_key=""):
    names = dict(CompanyConfigRecord.objects.values_list("company_key", "display_name"))
    jobs = RunJob.objects.filter(scope__in=[RunJob.SCOPE_PORTAL_REVIEW, RunJob.SCOPE_INVENTORY_PIPELINE, RunJob.SCOPE_INVENTORY_SYNC])
    if company_key:
        jobs = jobs.filter(company_key=company_key)
    out = []
    for job in jobs.order_by("-created_at")[:30]:
        label, _, tone = job_message(job)
        if job.status == RunJob.STATUS_SUCCEEDED:
            label, tone = "Finished", "neutral"
        out.append(dict(company=names.get(job.company_key, job.company_key),
            title="Review decision" if job.scope == RunJob.SCOPE_PORTAL_REVIEW else "Stock check",
            label=label, tone=tone, when=job.created_at, url=reverse("epos_qbo:run-detail", args=[job.id])))
    return out


def home_context(company_key="", now=None, token_health=None):
    now = now or timezone.now()
    expected = get_target_trading_date(now)
    window_start = expected - timedelta(days=29)
    companies = list(CompanyConfigRecord.objects.filter(is_active=True).order_by("display_name"))
    if company_key:
        companies = [c for c in companies if c.company_key == company_key]
    items, errors = inbox()
    runs = [r for r in ops.list_runs() if not r.dry_run]
    rows, banners, total, confirmed_count, amounts = [], [], Decimal(0), 0, []
    for company in companies:
        key = company.company_key
        waiting = len(items) if key == "company_a" else 0
        if key == "company_a":
            start = max(window_start, date(2026, 10, 1))
            confirmed = {}
            for run in runs:
                day = date_value(run.business_date)
                if day and day <= expected and verified_sales(run) and day not in confirmed:
                    confirmed[day] = money(run.step("sales").counts.get("qbo_total"))
            latest = max(confirmed, default=None)
            hold = ops.posting_hold()
            issue = "Sales are paused. Review the reason before allowing them to run again." if hold["active"] else ""
            schedule = ops.schedule_info(now)
            next_run = schedule["next_run"]
            scheduled = schedule["enabled"]
        else:
            start = window_start
            confirmed = {}
            known = RunArtifact.objects.filter(company_key=key, kind=RunArtifact.KIND_SALES_UPLOAD, target_date__lte=expected, reconcile_status="MATCH").exclude(upload_stats_json__dry_run=True)
            latest_record = known.order_by("-target_date", "-processed_at", "-id").first()
            for artifact in known.filter(target_date__gte=start).order_by("-target_date", "-processed_at", "-id"):
                if confirmed_artifact(artifact):
                    confirmed.setdefault(artifact.target_date, money(artifact.reconcile_qbo_total))
            latest = latest_record.target_date if latest_record else None
            job = RunJob.objects.filter(company_key=key, scope=RunJob.SCOPE_SINGLE).order_by("-created_at").first()
            issue = "The latest sales attempt failed. Check the details before trying again." if job and job.status == RunJob.STATUS_FAILED else ""
            if job and job.status == RunJob.STATUS_RUNNING and job.started_at and now - job.started_at > timedelta(hours=2):
                issue = "The sales run has been running for over two hours. Check it before starting another."
            schedules = RunSchedule.objects.filter(enabled=True, completed_at__isnull=True, scope__in=[RunJob.SCOPE_SINGLE, RunJob.SCOPE_ALL]).filter(Q(company_key=key) | Q(scope=RunJob.SCOPE_ALL))
            next_run = schedules.exclude(next_fire_at=None).order_by("next_fire_at").values_list("next_fire_at", flat=True).first()
            scheduled = schedules.exists()
        missing = [start + timedelta(days=i) for i in range(max(0, (expected - start).days + 1)) if start + timedelta(days=i) not in confirmed]
        if missing and not issue:
            issue = f"{len(missing)} days have no confirmed sales record in the checked period. Review the missing days."
        if key == "company_a" and errors and not issue:
            issue = "Some review records could not be read. Check Needs your attention."
        connection = (token_health or {}).get(key, {})
        if connection.get("severity") == "critical":
            connection_issue = "QuickBooks connection needs attention. Check the connection in Admin."
            issue = f"{issue} {connection_issue}".strip()
        label = "Needs attention" if issue else "Needs your decision" if waiting else "Up to date" if latest else "Not checked yet"
        history_url = reverse("epos_qbo:runs") + "?" + urlencode({"company": key})
        if connection.get("severity") == "critical":
            action_url, action_label = reverse("epos_qbo:api-tokens"), "Check connection"
        elif key == "company_a" and hold["active"]:
            action_url, action_label = reverse("epos_qbo:attention"), "Review sales pause"
        elif missing or (issue and not (key == "company_a" and errors)):
            action_url, action_label = history_url, "Review days"
        elif waiting or (key == "company_a" and errors):
            action_url, action_label = reverse("epos_qbo:attention"), f"Review {waiting} items" if waiting else "Review records"
        else:
            action_url, action_label = history_url, "View daily runs"
        row = dict(company_key=key, company=company.display_name, latest=latest, waiting=waiting,
            missing=missing, coverage_start=start, expected=expected, issue=issue, label=label,
            tone="danger" if issue else "warning" if waiting else "success" if latest else "neutral",
            next_run=next_run, scheduled=scheduled, url=history_url,
            company_url=reverse("epos_qbo:company-detail", args=[key]),
            action_url=action_url, action_label=action_label)
        rows.append(row)
        if issue:
            banners.append(dict(company=company.display_name, reason=issue, url=row["action_url"], action_label=action_label))
        if expected in confirmed:
            confirmed_count += 1
            amounts.append(confirmed[expected])
            if confirmed[expected] is not None:
                total += confirmed[expected]
    amount_known = bool(rows) and confirmed_count == len(rows) and all(a is not None for a in amounts)
    return dict(home_rows=rows, home_banners=banners, home_checked=now, home_expected=expected,
        home_sales=money_label(total) if amount_known else "Not fully confirmed", home_confirmed_count=confirmed_count,
        home_company_total=len(rows), home_waiting=sum(r["waiting"] for r in rows),
        home_missing=sum(len(r["missing"]) for r in rows), home_errors=errors if any(r["company_key"] == "company_a" for r in rows) else [])
