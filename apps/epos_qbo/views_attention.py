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
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from .models import PortalReviewAction, RunJob
from .services import attention, attention_actions, job_runner
from .views_company_a import _shell_context

SALT = "company-a-review-confirmation"
ITEM_ACTIONS = {"approve", "skip", "exclude", "preview", "routine", "repeat_ok"}
REASON_MAX = 300
ACTION_LABELS = {"approve": "Approved", "skip": "Skipped", "exclude": "Don't ask again", "unexclude": "Ask again",
                 "routine": "Approved as routine supplier", "repeat_ok": "Confirmed real repeat order",
                 "preview": "QuickBooks check", "daily": "Daily run"}

EXCLUDE_TEXT = {
    "product": "Don't ask about this product again. It won't be set up in QuickBooks. If it is sold on the till, "
               "that day's sales still stop until it is set up.",
    "vendor": "Don't ask about this supplier again. It is never created automatically, and its bills wait with "
              "“supplier excluded”.",
    "bill": "Handled outside the portal: this PO will never be posted as a bill. Use this only when the bill was "
            "entered or resolved some other way.",
    "routine_repeat": "Repeat orders from this supplier are routine (like daily bread). From now on, a repeat order "
                      "under ₦50,000 posts automatically and is only noted; a larger repeat still waits for you.",
}
BUTTONS = {
    ("approve", "product"): "Approve product", ("approve", "vendor"): "Approve supplier",
    ("approve", "bill"): "Approve bill", ("approve", "deposit"): "Approve deposit", ("approve", "hold"): "Clear hold",
    ("preview", "vendor"): "Check with QuickBooks", ("skip", "bill"): "Skip for this plan",
    ("routine", "bill"): "Approve · routine supplier", ("repeat_ok", "bill"): "Approve repeat order", ("exclude", "bill"): "Handled outside, never post",
}


def permission(user, action):
    return user.has_perm("epos_qbo.can_trigger_runs" if action == "daily" else "epos_qbo.can_approve_company_a_reviews")


def approval_reference(user):
    return attention_actions.approval_reference(user)


@login_required
@require_GET
def inbox(request):
    items, errors = attention.inbox()
    context = _shell_context([{"label": "Needs your attention", "url": None}])
    recent = list(PortalReviewAction.objects.select_related("job").order_by("-created_at")[:20])
    for record in recent:
        record.action_label = ACTION_LABELS.get(record.action, record.action.title())
    context.update(items=items, errors=errors, blockers=attention.blockers(), recent_actions=recent,
                   can_approve=permission(request.user, "approve"), can_run=permission(request.user, "daily"))
    return render(request, "epos_qbo/attention.html", context)


def item_facts(item):
    """Plain facts shown before confirming, so nobody has to open a file to decide."""
    extra, facts = item["extra"], []
    if item["kind"] == "product":
        target = extra.get("target") or {}
        facts += [("EPOS product", f"{extra.get('name')} (EPOS {extra.get('pid')})"),
                  ("QuickBooks item", f"{target.get('name', '')} {('(' + target['sku'] + ')') if target.get('sku') else ''}".strip() or "Not set")]
        if extra.get("with_master"):
            facts.append(("Also set up", f"{extra['with_master']['name']} (EPOS {extra['with_master']['pid']}), its main product"))
        if extra.get("stock") not in ("", None):
            facts.append(("EPOS stock now", str(extra["stock"])))
    elif item["kind"] == "vendor":
        vendor = extra.get("vendor") or {}
        facts.append(("EPOS supplier", vendor.get("epos_name", "")))
        if vendor.get("po_refs"):
            facts.append(("Purchase orders", ", ".join(map(str, vendor["po_refs"][:8]))))
        check = extra.get("check") or {}
        if check:
            facts.append(("Last QuickBooks check", describe_check(check)))
    elif item["kind"] == "bill":
        row = extra.get("row") or {}
        facts += [("Supplier", row.get("EPOS Supplier", "")), ("PO", item["identity"])]
        for label in ("Total", "Amount", "Total Inc", "Bill Date"):
            if row.get(label):
                facts.append((label, row[label]))
    elif item["kind"] == "deposit":
        summary = extra.get("summary") or {}
        from .services import workspace_records as records
        facts += [("Day", item["identity"]), ("Sales", records.money(summary.get("receipts_total"))),
                  ("Till sheet", records.money(summary.get("sheet_total")))]
    return facts


def describe_check(check):
    result = check.get("result") or {}
    requested = check.get("requested") or {}
    payload = result.get("payload") or {}
    if result.get("result") == "already_approved":
        return "Already approved. Refresh the inbox."
    if result.get("problems"):
        return "QuickBooks check found a problem: " + "; ".join(map(str, result["problems"]))
    if requested.get("mode") == "create":
        name = (payload.get("qbo_payload") or {}).get("DisplayName") or requested.get("display_name") or ""
        return f"Ready to create a new QuickBooks supplier named “{name}”."
    return f"Ready to link to QuickBooks supplier {payload.get('qbo_vendor_name') or ''} (Id {payload.get('qbo_vendor_id') or requested.get('link_to')})."


def describe(action, item):
    kind = item["kind"]
    if action == "exclude":
        return EXCLUDE_TEXT[kind]
    if action == "skip":
        if kind == "bill":
            return "Skip this PO in this plan. No bill is posted for it, and the day counts as done."
        return "Put this aside for now. Nothing changes in QuickBooks, the mappings or the daily routine. A new plan brings it back."
    if action == "preview":
        return "Check with QuickBooks how this supplier would be set up. Nothing is created or linked yet: you approve after the check."
    if kind == "hold":
        return "Allow sales to post again. The hold is archived with your name and reason. This does not post any sales by itself; run missed days afterwards, oldest first."
    if kind == "product":
        return item["reason"] + " Only this product is set up (plus its main product when listed). The tool refuses if the plan or this product changed since you looked."
    if kind == "vendor":
        return describe_check(item["extra"].get("check") or {}) + " The tool repeats the QuickBooks check and refuses if anything changed."
    if kind == "bill" and action == "repeat_ok":
        return ("This PO looks like a repeat of an earlier one. Confirm it is a real, separate order: it then "
                "posts as an unpaid bill in the next daily run. Say why in the reason (for example, daily bread).")
    if kind == "bill" and action == "routine":
        return ("Post this bill as an unpaid bill, and mark the supplier as routine. "
                + EXCLUDE_TEXT["routine_repeat"])
    if kind == "bill":
        return "Post the bill for this PO to QuickBooks as an unpaid bill. Nobody is paid by this action."
    if kind == "deposit":
        return "Move this day's sales from Undeposited Funds into the banks, in the till sheet's amounts. The tool refuses if the plan changed."
    return item["reason"]


def _daily_data(request):
    from code_scripts.akponora_ops.daily_run import last_closed_business_date
    try:
        target = date.fromisoformat(request.GET.get("date") or last_closed_business_date().isoformat())
    except ValueError:
        raise ValueError("Choose a valid business date.")
    if not date(2026, 10, 1) <= target <= last_closed_business_date():
        raise ValueError("Choose a closed business day from 1 October 2026.")
    only = request.GET.get("only", "")
    if only not in ("", "catalogue", "bills", "sales", "guard", "uf"):
        raise ValueError("Choose a supported daily step.")
    data = {"title": f"Akponora daily run · {target}", "date": target.isoformat(), "dry_run": request.GET.get("mode", "dry") != "post", "only": only}
    title = f"{'Preview' if data['dry_run'] else 'Run'} Akponora for {target:%-d %B %Y}"
    description = "This preview plans the selected steps and posts nothing to QuickBooks." if data["dry_run"] else "This runs the existing daily routine. Enabled steps may create products and suppliers, post unpaid bills, sales receipts and deposits through their existing approval gates."
    return data, title, description, title.split(" Akponora")[0]


def _list_data(request, action):
    """Don't-ask-again changes that are not tied to an inbox card."""
    from .services import exclusions, products
    kind = request.GET.get("kind", "")
    if action == "unexclude":
        row = exclusions.find(kind, request.GET.get("ex", ""))
        if not row:
            raise ValueError("This entry is not on the don't-ask-again list.")
        title = f"Ask again about {row['kind_label'].lower()} {row['key']}"
        description = (f"Remove {row['kind_label'].lower()} {row['key']} from the don't-ask-again list. "
                       "The next daily check plans it again and it can come back to this inbox.")
        return {"title": title, "exclusion_kind": row["kind"], "exclusion_key": row["key"]}, title, description, "Remove from list"
    pid = request.GET.get("pid", "")
    if kind != "product" or pid not in products.unmapped_ids() or exclusions.is_excluded("product", pid):
        raise ValueError("This product is not waiting to be set up.")
    name = products.unmapped_name(pid)
    title = f"Don't ask again · {name}"
    return {"title": title, "exclusion_kind": "product", "exclusion_key": pid}, title, EXCLUDE_TEXT["product"], "Don't ask again"


@login_required
@require_http_methods(["GET", "POST"])
def confirm(request):
    if request.method == "POST":
        return _confirm_post(request)
    action = request.GET.get("action", "approve")
    if action not in ITEM_ACTIONS | {"daily", "unexclude"}:
        return HttpResponseBadRequest("Unknown action")
    if not permission(request.user, action):
        return HttpResponseForbidden("You do not have permission for this action.")
    item, facts = None, []
    try:
        if action == "daily":
            data, title, description, button = _daily_data(request)
        elif action == "unexclude" or (action == "exclude" and not request.GET.get("key")):
            data, title, description, button = _list_data(request, action)
        else:
            items, _ = attention.inbox()
            item = next((i for i in items if i["key"] == request.GET.get("key")), None)
            if not item or not (item.get(action) or (action in ("routine", "repeat_ok") and item["extra"].get(action))):
                raise ValueError("This action is unavailable. Fix the source and refresh the inbox.")
            data = {"key": item["key"], "snapshot": item["snapshot"], "title": item["title"], "kind": item["kind"],
                    "identity": item["identity"], "business_date": item["date"], "run_id": item["run"].run_id if item["run"] else "",
                    "relative": item["relative"], "tool_sha": item["extra"].get("sha", "")}
            label = BUTTONS.get((action, item["kind"])) or ("Don't ask again" if action == "exclude" else "Skip for now" if action == "skip" else "Confirm")
            title, description, button, facts = f"{label} · {item['title']}", describe(action, item), label, item_facts(item)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    data.update(action=action, user=request.user.pk, nonce=uuid.uuid4().hex)
    token = signing.dumps(data, salt=SALT)
    context = _shell_context([{"label": "Needs your attention", "url": reverse("epos_qbo:attention")}, {"label": "Confirm", "url": None}])
    context.update(title=title, description=description, item=item, token=token, action=action, facts=facts,
                   button=button, approval_actor=attention_actions.actor_name(request.user),
                   approval_preview=approval_reference(request.user), reason_max=REASON_MAX)
    return render(request, "epos_qbo/attention_confirm.html", context)


def _vendor_choice(request, item):
    choice = request.POST.get("choice", "")
    if choice == "create":
        name = " ".join(request.POST.get("display_name", "").split())[:100]
        return {"mode": "create", "display_name": name}
    if choice.startswith("link:") and choice[5:] in {c["id"] for c in item["extra"]["candidates"]}:
        return {"mode": "link", "link_to": choice[5:]}
    raise ValueError("Choose one of the listed QuickBooks suppliers, or create a new one.")


def _confirm_post(request):
    token = request.POST.get("token", "")
    try:
        data = signing.loads(token, salt=SALT, max_age=3600)
        action = data["action"]
        if data["user"] != request.user.pk or not permission(request.user, action):
            return HttpResponseForbidden("You do not have permission for this confirmation.")
        reason = " ".join(request.POST.get("reason", "").split())
        if not reason or not request.POST.get("confirmed"):
            return HttpResponseBadRequest("Confirm the action and write a short reason.")
        if len(reason) > REASON_MAX:
            return HttpResponseBadRequest(f"Keep the reason under {REASON_MAX} characters.")
        if data.get("key"):
            item = attention_actions.current_item(data["key"], data["snapshot"])
            if not item.get(action):
                raise ValueError("This action is no longer available")
            if action == "preview":
                data["choice"] = _vendor_choice(request, item)
        confirmation_id = hashlib.sha256(token.encode()).hexdigest()
        try:
            with transaction.atomic():
                # Duplicate submissions of the same confirmation create only one job.
                existing = PortalReviewAction.objects.filter(confirmation_id=confirmation_id).first()
                if existing:
                    return redirect("epos_qbo:run-detail", job_id=existing.job_id)
                job = RunJob.objects.create(scope=RunJob.SCOPE_PORTAL_REVIEW, company_key="company_a", requested_by=request.user,
                                            target_date=data.get("date"), status=RunJob.STATUS_QUEUED)
                data["approval_ref"] = approval_reference(request.user)
                PortalReviewAction.objects.create(job=job, actor=request.user.get_username(), action=action, reason=reason,
                                                  payload=data, confirmation_id=confirmation_id)
        except IntegrityError:
            existing = PortalReviewAction.objects.get(confirmation_id=confirmation_id)
            return redirect("epos_qbo:run-detail", job_id=existing.job_id)
    except (signing.BadSignature, ValueError, KeyError) as exc:
        return HttpResponseBadRequest(str(exc))
    # Existing dispatcher starts a monitored subprocess, never runs the tool in this request.
    job_runner.dispatch_next_queued_job()
    messages.success(request, "Queued. The result and your decision are recorded under Recent decisions.")
    return redirect("epos_qbo:run-detail", job_id=job.id)
