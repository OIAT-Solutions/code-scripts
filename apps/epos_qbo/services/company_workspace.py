"""Company tabs over existing records; no remote calls, edits or posting tools."""
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.urls import reverse

from ..models import CompanyConfigRecord
from . import attention, company_a_ops as ops, experience

TAB_LABELS = {"overview": "Overview", "attention": "Review queue", "sales": "Sales",
              "purchases": "Purchases", "products": "Products & stock", "deposits": "Banking",
              "runs": "Daily runs", "schedules": "Schedules", "settings": "Settings"}
STEP_TABS = {"purchases": {"bills"}, "products": {"catalogue", "guard"}, "deposits": {"uf", "deposits", "uf_deposits"}}
KIND_TABS = {"purchases": {"bill", "vendor"}, "products": {"product", "stock_movement", "stock_adjust", "stock_follow"}, "deposits": {"deposit"}}
QUEUE_KIND_GROUPS = {"stock": {"stock_movement", "stock_adjust", "stock_follow"},
                     "purchases": {"bill", "vendor"}}


def available_tabs(company, user, inventory_enabled=False):
    if company.company_key == ops.COMPANY_KEY:
        return ["overview", "attention", "sales", "purchases", "products", "deposits", "runs", "schedules", "settings"]
    tabs = ["sales"]
    if user.has_perm("epos_qbo.can_edit_companies"):
        tabs += ["settings"]
    return tabs


def page_context(company, request, inventory_enabled=False, token_health=None):
    key = company.company_key
    tabs = available_tabs(company, request.user, inventory_enabled)
    active = request.GET.get("tab", "overview" if key == ops.COMPANY_KEY else "sales")
    if active not in tabs:
        active = "sales"
    base = reverse("epos_qbo:company-detail", args=[key])
    # Switching company retains the tab only when that company supports it.
    from ..views import _company_inventory_enabled
    choices = []
    for other in CompanyConfigRecord.objects.filter(is_active=True).order_by("display_name"):
        allowed = available_tabs(other, request.user, _company_inventory_enabled(other))
        choices.append(dict(name=other.display_name, key=other.company_key,
            url=reverse("epos_qbo:company-detail", args=[other.company_key]) + "?" + urlencode({"tab": active if active in allowed else "sales"})))
    home = experience.home_context(key, token_health=token_health)
    position = next(iter(home["home_rows"]), None)
    history = experience.daily_rows(key, include_previews=False) if active in {"overview", "attention", "sales", "runs"} or (key != ops.COMPANY_KEY and active == "sales") else []
    all_history = list(history)
    sales_query = request.GET.get("q", "").strip()[:160].casefold()
    sales_filter = request.GET.get("status", "all")
    if sales_filter not in {"all", "confirmed", "attention", "pending"}:
        sales_filter = "all"
    history = [row for row in history if
        (not sales_query or sales_query in " ".join([str(row.get("day") or ""), row.get("label", ""), row.get("latest", {}).get("message", "")]).casefold())
        and (sales_filter == "all" or sales_filter == "confirmed" and row["confirmed"]
             or sales_filter == "attention" and (row["latest_issue"] or row["tone"] in {"danger", "warning"})
             or sales_filter == "pending" and not row["confirmed"])]
    sales_page = Paginator(history, 15).get_page(request.GET.get("page"))
    raw_decisions, errors = attention.inbox() if key == ops.COMPANY_KEY else ([], [])
    all_decisions = [dict(i, day=experience.date_value(i["date"])) for i in raw_decisions]
    decisions = [i for i in all_decisions if active == "attention" or i["kind"] in KIND_TABS.get(active, set())]
    queue_query = request.GET.get("q", "").strip()[:160].casefold() if active in {"attention", "purchases", "products", "deposits"} else ""
    queue_filter = request.GET.get("kind", "all")
    if queue_filter != "all":
        kinds = QUEUE_KIND_GROUPS.get(queue_filter, {queue_filter})
        decisions = [i for i in decisions if i["kind"] in kinds]
    if queue_query:
        decisions = [i for i in decisions if queue_query in " ".join([i.get("title", ""), i.get("reason", ""), i.get("kind", ""), i.get("identity", "")]).casefold()]
    queue_page = Paginator(decisions, 15).get_page(request.GET.get("queue_page"))
    register_context = {}
    if key == ops.COMPANY_KEY and active == "products":
        from .products import context as products_context
        register_context.update(products_context(request))
    if key == ops.COMPANY_KEY and active in {"deposits", "settings"}:
        from .deposits import context as deposits_context
        register_context.update(deposits_context(request))
    if key == ops.COMPANY_KEY and active == "settings":
        from ..models import PortalSettingChange
        register_context["setting_changes"] = PortalSettingChange.objects.filter(company_key=key)[:10]
    register_context["can_approve"] = request.user.has_perm("epos_qbo.can_approve_company_a_reviews")
    from ..models import RunSchedule
    nora_schedules = []
    if key == ops.COMPANY_KEY:
        from .. import views as portal_views
        schedules = list(RunSchedule.objects.filter(company_key=ops.COMPANY_KEY).order_by("name", "created_at"))
        normalized = portal_views._schedule_rows(schedules, {ops.COMPANY_KEY: company.display_name})
        nora_schedules = [row for row in normalized if row["schedule"].company_key == ops.COMPANY_KEY]
    nora_run_rows = all_history
    category_labels = {"stock": "Stock changes", "product": "Products", "purchases": "Purchase orders",
                       "deposit": "Banking", "hold": "Sales pause", "flag": "Other records"}
    decision_counts = {kind: sum(1 for item in all_decisions if item["kind"] in QUEUE_KIND_GROUPS.get(kind, {kind}))
                       for kind in category_labels}
    trend_rows = []
    if key == ops.COMPANY_KEY:
        recent_days = [row for row in all_history if row.get("day")][:7]
        max_value = max((row["amount"] for row in recent_days if row.get("confirmed") and row.get("amount") is not None), default=None)
        for row in reversed(recent_days):
            amount = row.get("amount") if row.get("confirmed") else None
            height = (0 if amount == 0 else max(6, int(float(amount / max_value) * 100))) if amount is not None and max_value else 0
            trend_rows.append(dict(day=row["day"], amount=row["amount_label"] if amount is not None else "Not confirmed",
                                   height=height, confirmed=amount is not None))
    register_context.update(company_queue_page=queue_page, company_queue_count=len(decisions),
        company_queue_query=queue_query, company_queue_filter=queue_filter, company_sales_query=sales_query,
        company_sales_filter=sales_filter, company_sales_count=len(history), company_nora_schedules=nora_schedules,
        company_nora_runs=nora_run_rows, company_all_decisions=all_decisions,
        company_decision_counts=decision_counts,
        nora_categories=[dict(kind=kind, label=label, count=decision_counts[kind])
                         for kind, label in category_labels.items() if decision_counts[kind]],
        nora_sales_trend=trend_rows,
        nora_workspace=(key == ops.COMPANY_KEY))
    return dict(**register_context, company_position=position, company_tabs=[dict(key=t, label=TAB_LABELS[t], url=base + "?" + urlencode({"tab":t})) for t in tabs],
        company_key=key, company_tab=active, company_tab_label=TAB_LABELS[active], company_choices=choices,
        company_sales_page=sales_page, company_decisions=decisions, company_record_errors=errors,
        company_history_url=reverse("epos_qbo:runs") + "?" + urlencode({"company":key}),
        company_tab_query=urlencode({"tab":active}))
