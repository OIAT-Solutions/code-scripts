from django.contrib import messages
from django.contrib.auth.decorators import login_required,permission_required
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404,redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from .models import CompanyConfigRecord,RunJob
from .services import workspace_jobs,job_runner


@login_required
@permission_required("epos_qbo.can_trigger_runs",raise_exception=True)
@require_POST
def update(request,company_key):
    company=get_object_or_404(CompanyConfigRecord,company_key=company_key,is_active=True)
    if company_key!="company_a":return HttpResponseBadRequest("This company does not support this update")
    action=request.POST.get("action","");day=request.POST.get("day","")
    try:workspace_jobs.command(action,"validation",day)
    except ValueError as exc:return HttpResponseBadRequest(str(exc))
    with transaction.atomic():
        CompanyConfigRecord.objects.select_for_update().get(pk=company.pk)
        active=RunJob.objects.filter(scope=RunJob.SCOPE_WORKSPACE_READ,company_key=company_key,status__in=["queued","running"],inventory_options_json__action=action).exists()
        if active:messages.info(request,"This update is already waiting or running.")
        else:
            RunJob.objects.create(scope=RunJob.SCOPE_WORKSPACE_READ,company_key=company_key,requested_by=request.user,inventory_options_json={"action":action,"day":day})
            transaction.on_commit(job_runner.dispatch_next_queued_job)
            messages.success(request,"Update queued. The last successful records stay available while it runs.")
    return redirect(reverse("epos_qbo:company-detail",args=[company_key])+"?tab="+("products" if action=="stock" else "deposits"))
