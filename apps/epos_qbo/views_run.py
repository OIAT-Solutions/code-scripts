"""One way to start a run, for every company (Marvin, 6 Oct 2026).

The Run dialog on Daily runs asks for the company, the business day and what to run; the choices follow
the company's workflows (services/workflows.WORKFLOWS). ``run_review`` shows what will happen before
anything starts: the Daily routine goes to the audited confirm page; a sales sync gets its own review page
that posts to the existing ``run-trigger`` endpoint.
"""
from __future__ import annotations

from datetime import date

from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_GET

from .models import CompanyConfigRecord, RunJob
from .services import company_a_ops as ops
from .services import workflows

SALES = "sales"


def run_options() -> dict:
    """Per active company: its name, what can be run, and whether a preview exists."""
    out = {}
    for c in CompanyConfigRecord.objects.filter(is_active=True).order_by("display_name"):
        if workflows.WORKFLOWS[RunJob.SCOPE_COMPANY_A_DAILY].available_for(c.company_key):
            steps, preview = ops.DAILY_STEP_CHOICES, True
        else:
            steps, preview = [(SALES, "Sales")], False
        out[c.company_key] = {"name": c.display_name, "steps": [list(s) for s in steps], "preview": preview}
    return out


def last_closed_day() -> date:
    from code_scripts.akponora_ops.daily_run import last_closed_business_date

    return last_closed_business_date()


def dialog_context(request) -> dict:
    """Context for components/run_dialog.html; ``run_company`` in the URL opens it prefilled."""
    company = request.GET.get("run_company", "")
    options = run_options()
    return {"run_options": options, "run_default_day": last_closed_day().isoformat(),
            "run_prefill": {"company": company if company in options else "",
                            "step": request.GET.get("run_step", ""), "day": request.GET.get("run_date", ""),
                            "open": company in options}}


@login_required
@permission_required("epos_qbo.can_trigger_runs", raise_exception=True)
@require_GET
def run_review(request):
    options = run_options()
    company = request.GET.get("company", "")
    back = reverse("epos_qbo:runs")
    if company not in options:
        return redirect(back)
    day = request.GET.get("date") or last_closed_day().isoformat()
    step = request.GET.get("only", "")
    mode = request.GET.get("mode", "dry")
    if options[company]["preview"]:  # the Daily routine: the audited confirm page
        query = urlencode({"action": "daily", "date": day, "only": step, "mode": mode})
        return redirect(f"{reverse('epos_qbo:attention-confirm')}?{query}")
    try:
        target = date.fromisoformat(day)
    except ValueError:
        return redirect(back)
    name = options[company]["name"]
    return render(request, "epos_qbo/run_review.html", {
        "title": f"Run sales for {name}, {target:%-d %B %Y}",
        "description": f"Downloads that day's EPOS sales for {name}, posts them to QuickBooks and checks the totals "
                       "match. Days already in QuickBooks are not posted twice.",
        "company_key": company, "target_date": target.isoformat(),
        "breadcrumbs": [{"label": "Home", "url": reverse("epos_qbo:overview")},
                        {"label": "Daily runs", "url": back}, {"label": "Review", "url": None}],
        "back_url": back, "back_label": "Daily runs",
    })
