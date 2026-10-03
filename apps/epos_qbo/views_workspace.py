"""Queue read-only records updates (stock snapshot, till-sheet status, deposit re-plan)."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import CompanyConfigRecord, RunJob
from .services import job_runner, workspace_jobs


@login_required
@permission_required("epos_qbo.can_trigger_runs", raise_exception=True)
@require_POST
def update(request, company_key):
    company = get_object_or_404(CompanyConfigRecord, company_key=company_key, is_active=True)
    if company_key != "company_a":
        return HttpResponseBadRequest("This company does not support this update")
    action = request.POST.get("action", "")
    day = request.POST.get("day", "") if action == "deposit_plan" else ""
    mode = request.POST.get("mode", "") if action == "stock" else ""
    try:
        workspace_jobs.command(action, "validation", day, mode)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    with transaction.atomic():
        CompanyConfigRecord.objects.select_for_update().get(pk=company.pk)
        active = RunJob.objects.filter(scope=RunJob.SCOPE_WORKSPACE_READ, company_key=company_key,
                                       status__in=[RunJob.STATUS_QUEUED, RunJob.STATUS_RUNNING],
                                       inventory_options_json__action=action).exists()
        if active:
            messages.info(request, "This update is already waiting or running.")
        else:
            RunJob.objects.create(scope=RunJob.SCOPE_WORKSPACE_READ, company_key=company_key, requested_by=request.user,
                                  inventory_options_json={"action": action, "day": day, "mode": mode})
            transaction.on_commit(job_runner.dispatch_next_queued_job)
            messages.success(request, "Update queued. The last good records stay on the page while it runs; reload when it finishes.")
    tab = "products" if action == "stock" else "deposits"
    suffix = f"&day={day}#deposit-details" if day else ""
    return redirect(reverse("epos_qbo:company-detail", args=[company_key]) + "?tab=" + tab + suffix)


@login_required
@permission_required("epos_qbo.can_manage_portal_settings", raise_exception=True)
@require_POST
def deposit_settings(request, company_key):
    from .services import deposits
    from .services.attention_actions import actor_name
    if company_key != "company_a":
        return HttpResponseBadRequest("This company has no deposit settings")
    reason = " ".join(request.POST.get("reason", "").split())[:300]
    if not reason:
        return HttpResponseBadRequest("Write a short reason for the change.")
    values = {"OIAT_COMPANY_A_UF_TOLERANCE": request.POST.get("tolerance", ""),
              "OIAT_COMPANY_A_UF_TOLERANCE_PCT": request.POST.get("tolerance_pct", "")}
    try:
        changed = deposits.save_tolerance(values, actor=f"{actor_name(request.user)} ({request.user.get_username()})", reason=reason)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    messages.success(request, "Deposit tolerance saved. The next deposit run uses it." if changed else "Nothing changed.")
    return redirect(reverse("epos_qbo:company-detail", args=[company_key]) + "?tab=settings#deposit-settings")
