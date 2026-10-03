"""Inbox and explicit confirmations; views only queue existing tools, never call QBO."""
from __future__ import annotations

import hashlib
import uuid
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction, IntegrityError
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from .models import PortalReviewAction, RunJob
from .services import attention, attention_actions, job_runner
from .views_company_a import _shell_context

SALT = "company-a-review-confirmation"


def permission(user, action):
    return user.has_perm("epos_qbo.can_trigger_runs" if action == "daily" else "epos_qbo.can_approve_company_a_reviews")


def needs_approval_reference(data):
    return not ((data["action"] == "daily" and data["dry_run"]) or (data["action"] == "skip" and data.get("kind") != "bill"))


@login_required
@require_GET
def inbox(request):
    items, errors = attention.inbox()
    context = _shell_context([{"label": "Needs your attention", "url": None}])
    context.update(items=items, errors=errors, blockers=attention.blockers(),
                   recent_actions=PortalReviewAction.objects.select_related("job").order_by("-created_at")[:20],
                   can_approve=permission(request.user, "approve"), can_run=permission(request.user, "daily"))
    return render(request, "epos_qbo/attention.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def confirm(request):
    item = None
    if request.method == "GET":
        action = request.GET.get("action", "approve")
        if not permission(request.user, action):
            return HttpResponseForbidden("You do not have permission for this action.")
        if action == "daily":
            from code_scripts.akponora_ops.daily_run import last_closed_business_date
            try:
                target = date.fromisoformat(request.GET.get("date") or last_closed_business_date().isoformat())
            except ValueError:
                return HttpResponseBadRequest("Choose a valid business date.")
            if not date(2026, 10, 1) <= target <= last_closed_business_date():
                return HttpResponseBadRequest("Choose a closed business day from 1 October 2026.")
            only = request.GET.get("only", "")
            if only not in ("", "catalogue", "bills", "sales", "guard", "uf"):
                return HttpResponseBadRequest("Choose a supported daily step.")
            data = {"title": f"Akponora daily run · {target}", "date": target.isoformat(), "dry_run": request.GET.get("mode", "dry") != "post", "only": only}
            title = f"{'Preview' if data['dry_run'] else 'Run'} Akponora for {target}"
            description = "This preview plans the selected steps and posts nothing to QuickBooks." if data["dry_run"] else "This runs the existing daily routine. Enabled steps may create products and suppliers, post unpaid bills, sales receipts and deposits through their existing approval gates."
        else:
            if action not in {"approve", "skip"}:
                return HttpResponseBadRequest("Unknown action")
            items, _ = attention.inbox()
            item = next((i for i in items if i["key"] == request.GET.get("key")), None)
            if not item or not item[action]:
                return HttpResponseBadRequest("This action is unavailable. Fix the source and refresh the inbox.")
            data = {"key": item["key"], "snapshot": item["snapshot"], "title": item["title"], "kind": item["kind"], "identity": item["identity"], "business_date": item["date"], "run_id": item["run"].run_id if item["run"] else "", "relative": item["relative"], "tool_sha": item["extra"].get("sha", "")}
            title = ("Clear sales hold" if item["kind"] == "hold" else f"{action.title()} · {item['title']}")
            description = "Archive the posting hold with your name and reason. This does not retry sales; reconcile existing receipts before running missed days." if item["kind"] == "hold" else ("Mark this PO resolved outside the tool. It will be skipped and the cursor may advance. No bill will be posted for this PO." if action == "skip" else item["reason"])
        if action == "skip" and item and item["kind"] != "bill":
            description = "Defer this evidence in the inbox. No QuickBooks transaction, mapping or pipeline cursor changes. A new plan will bring the item back for review."
        if action == "approve" and item and item["kind"] == "vendor":
            description = "Link this EPOS supplier to the existing QuickBooks supplier you choose below. The next bills preview uses that mapping. No new supplier or bill is created by this action."
        data.update(action=action, user=request.user.pk, nonce=uuid.uuid4().hex)
        token = signing.dumps(data, salt=SALT)
    else:
        token = request.POST.get("token", "")
        try:
            data = signing.loads(token, salt=SALT, max_age=3600)
            action = data["action"]
            if data["user"] != request.user.pk or not permission(request.user, action):
                return HttpResponseForbidden("You do not have permission for this confirmation.")
            reason = request.POST.get("reason", "").strip()
            approval_ref = request.POST.get("approval_ref", "").strip()
            if not reason or not request.POST.get("confirmed") or (needs_approval_reference(data) and not approval_ref):
                return HttpResponseBadRequest("Confirm the action, enter a reason and the specific chat approval reference.")
            if action != "daily":
                item = attention_actions.current_item(data["key"], data["snapshot"])
                if not item[action]:
                    raise ValueError("This action is no longer available")
            if action == "approve" and item and item["kind"] == "vendor":
                vendor_id = request.POST.get("vendor_id", "")
                if vendor_id not in {c["id"] for c in item["extra"]["candidates"]}:
                    return HttpResponseBadRequest("Choose one of the reviewed existing suppliers")
                data["vendor_id"] = vendor_id
            confirmation_id = hashlib.sha256(token.encode()).hexdigest()
            try:
                with transaction.atomic():
                    # Duplicate submissions of the same confirmation create only one job.
                    existing = PortalReviewAction.objects.filter(confirmation_id=confirmation_id).first()
                    if existing:
                        return redirect("epos_qbo:run-detail", job_id=existing.job_id)
                    job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW, company_key="company_a", requested_by=request.user,
                                               target_date=data.get("date"), status=RunJob.STATUS_QUEUED)
                    data["approval_ref"] = approval_ref
                    PortalReviewAction.objects.create(job=job, actor=request.user.get_username(), action=action, reason=reason,
                                                      payload=data, confirmation_id=confirmation_id)
            except IntegrityError:
                existing = PortalReviewAction.objects.get(confirmation_id=confirmation_id)
                return redirect("epos_qbo:run-detail", job_id=existing.job_id)
        except (signing.BadSignature, ValueError, KeyError) as exc:
            return HttpResponseBadRequest(str(exc))
        # Existing dispatcher starts a monitored subprocess, never runs the tool in this request.
        job_runner.dispatch_next_queued_job()
        messages.success(request, "Action queued. Its result and your approval are recorded in the inbox.")
        return redirect("epos_qbo:run-detail", job_id=job.id)
    context = _shell_context([{"label": "Confirm action", "url": None}])
    context.update(title=title, description=description, item=item, token=token, action=action,
                   needs_approval_ref=needs_approval_reference(data))
    return render(request, "epos_qbo/attention_confirm.html", context)
