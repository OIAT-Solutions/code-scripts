"""Read-only portal pages for the Company A (AKPONORA) daily run.

Phase 1 is visibility only: no view here writes anything, triggers a run or touches QBO.
Every view is GET-only and login-protected like the rest of the portal.
"""
from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from .services import company_a_ops as ops

LOGGER = logging.getLogger(__name__)


def _shell_context(crumbs: list[dict]) -> dict:
    from .views import _breadcrumb_context, _nav_context

    context = _nav_context()
    context.update(
        _breadcrumb_context(
            [{"label": "Home", "url": reverse("epos_qbo:overview")}, *crumbs],
            back_url=reverse("epos_qbo:runs"),
            back_label="Daily runs",
        )
    )
    return context


def _get_run_or_404(business_date: str, run_id: str) -> ops.Run:
    run = ops.get_run(business_date, run_id)
    if run is None:
        raise Http404("Company A run not found")
    return run


@login_required
@require_GET
def company_a_runs(request):
    show_dry = request.GET.get("dry", "1") != "0"
    runs = ops.list_runs(include_dry=True)
    context = _shell_context([
        {"label": "Runs", "url": reverse("epos_qbo:runs")},
        {"label": "Company A daily runs", "url": None},
    ])
    context.update({
        "runs": [r for r in runs if show_dry or not r.dry_run],
        "show_dry": show_dry,
        "schedule": ops.schedule_info(),
        "hold": ops.posting_hold(),
        "guard": ops.guard_alerts(runs),
        "daily_root": str(ops.daily_root()),
    })
    return render(request, "epos_qbo/company_a_runs.html", context)


@login_required
@require_GET
def company_a_run_detail(request, business_date: str, run_id: str):
    run = _get_run_or_404(business_date, run_id)
    from .services import experience, messages
    from .models import CompanyConfigRecord
    company = CompanyConfigRecord.objects.filter(company_key=ops.COMPANY_KEY).first()
    label, sentence, tone = experience.run_message(run)
    day = experience.date_value(run.business_date)
    day_rows = experience.daily_rows(ops.COMPANY_KEY, day, day, include_previews=False) if day else []
    other_confirmation = next((row for row in day_rows if row["confirmed"]), None) if not experience.verified_sales(run) else None
    steps = []
    field_labels = {"new_products": "New products checked", "items_created": "Products created",
        "posted": "Bills posted", "already_posted": "Already posted", "hold": "Waiting for review",
        "uploaded": "Sales receipts posted", "skipped": "Receipts skipped", "failed": "Receipts not posted",
        "qbo_total": "QuickBooks sales total", "epos_total": "Till sales total",
        "alert": "Stock problems", "warn": "Stock warnings", "balance": "Receipts awaiting deposit",
        "uf_balance": "Receipts awaiting deposit", "deposited": "Days recorded", "held": "Days needing follow-up",
        "last_deposit_date": "Last deposit day", "days_since_last_deposit": "Days since last deposit"}
    for step in run.steps:
        facts = []
        if step.status not in {"skipped", "disabled", "unknown"}:
            for field, caption in field_labels.items():
                if field in step.counts:
                    value = step.counts[field]
                    if isinstance(value, (list, tuple)):
                        value = len(value)
                    if field in {"qbo_total", "epos_total", "balance", "uf_balance"}:
                        value = experience.money_label(experience.money(value))
                    if run.dry_run and field in {"items_created", "posted", "uploaded", "deposited"}:
                        continue
                    facts.append((caption, value))
        steps.append({"step": step, "label": "Stock checks" if step.name == "guard" else step.label,
            "outcome": messages.step_outcome(step, run.dry_run), "facts": facts,
            "log_tail": ops.log_tail(run, step.name)})
    context = _shell_context([
        {"label": "Daily runs", "url": reverse("epos_qbo:runs") + "?company=company_a"},
        {"label": day.strftime("%d %B %Y").lstrip("0") if day else run.business_date, "url": None},
    ])
    groups = {}
    for file in ops.evidence_files(run):
        prefix = file["path"].split("/")[0]
        caption = {"sales": "Sales reconciliation", "bills": "Purchase records", "catalogue": "Product mapping", "guard": "Item checks", "stock": "Stock comparison", "uf": "Deposit records"}.get(prefix, "Other records")
        groups.setdefault(caption, []).append(file)
    context.update({
        "evidence_groups": [{"label": label, "files": files} for label, files in groups.items()],
        "run": run,
        "business_day": day,
        "other_confirmation": other_confirmation,
        "business_company": company.display_name if company else "Akponora",
        "business_outcome": {"label": label, "sentence": sentence, "tone": tone},
        "steps": steps,
        "files": ops.evidence_files(run),
        "hold": ops.posting_hold(),
    })
    return render(request, "epos_qbo/company_a_run_detail.html", context)


@login_required
@require_GET
def company_a_evidence(request, business_date: str, run_id: str):
    run = _get_run_or_404(business_date, run_id)
    rel = request.GET.get("path", "")
    try:
        path = ops.resolve_evidence(run, rel)
    except ops.EvidenceError as exc:
        LOGGER.warning("Refused Company A evidence path %r for %s/%s: %s", rel, business_date, run_id, exc)
        raise Http404("Evidence file not available") from None
    context = _shell_context([
        {"label": "Company A daily runs", "url": reverse("epos_qbo:company-a-runs")},
        {"label": f"{run.business_date} · {run.run_id}",
         "url": reverse("epos_qbo:company-a-run-detail", args=[run.business_date, run.run_id])},
        {"label": rel, "url": None},
    ])
    context.update({"run": run, "rel_path": rel, "content": ops.read_evidence(path)})
    return render(request, "epos_qbo/company_a_evidence.html", context)


def safe_overview_card() -> dict | None:
    """Context for the overview / company-detail Company A card; None when there is nothing to show."""
    try:
        card = ops.overview_card()
    except Exception:  # noqa: BLE001 - never break the dashboard
        LOGGER.exception("Company A overview card failed")
        return None
    if not (card["has_evidence"] or card["schedule"]["enabled"] or card["hold"]["active"]):
        return None
    return card


def safe_schedule_row() -> dict | None:
    try:
        return ops.schedule_row()
    except Exception:  # noqa: BLE001
        LOGGER.exception("Company A schedule row failed")
        return None


def safe_recent_runs(limit: int = 5) -> list:
    try:
        return ops.list_runs(limit=limit)
    except Exception:  # noqa: BLE001
        LOGGER.exception("Company A run list failed")
        return []


def safe_holds_alerts() -> dict | None:
    try:
        return {"hold": ops.posting_hold(), "guard": ops.guard_alerts()}
    except Exception:  # noqa: BLE001
        LOGGER.exception("Company A holds/alerts failed")
        return None
