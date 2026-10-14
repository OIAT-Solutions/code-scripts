"""Company tabs over existing records; no remote calls, edits or posting tools."""
from urllib.parse import urlencode
from decimal import Decimal, ROUND_CEILING

from django.core.paginator import Paginator
from django.urls import reverse

from ..models import CompanyConfigRecord
from . import attention, company_a_ops as ops, experience

TAB_LABELS = {"admin": "Admin", "overview": "Overview", "attention": "Review queue", "sales": "Sales",
              "purchases": "Purchases", "products": "Products & stock", "deposits": "Banking",
              "runs": "Daily runs", "schedules": "Schedules", "settings": "Settings"}
STEP_TABS = {"purchases": {"bills"}, "products": {"catalogue", "guard"}, "deposits": {"uf", "deposits", "uf_deposits"}}
KIND_TABS = {"purchases": {"bill", "vendor"}, "products": {"product", "stock_movement", "stock_adjust", "stock_follow"}, "deposits": {"deposit"}}
QUEUE_KIND_GROUPS = {"stock": {"stock_movement", "stock_adjust", "stock_follow"},
                     "purchases": {"bill", "vendor"}}


def tab_url(base, tab, key):
    """Nora Mart's Review queue is the table + side-panel page (attention.html), not a workspace tab."""
    if tab == "attention" and key == ops.COMPANY_KEY:
        return reverse("epos_qbo:attention")
    return base + "?" + urlencode({"tab": tab})


def available_tabs(company, user, inventory_enabled=False):
    if company.company_key == ops.COMPANY_KEY:
        return ["overview", "attention", "sales", "purchases", "products", "deposits", "runs", "schedules", "settings"]
    tabs = ["sales"]
    if user.has_perm("epos_qbo.can_edit_companies"):
        tabs += ["settings"]
    return tabs


def switcher_choices(request):
    """Default to each company's first supported tab; browser history may restore a valid tab."""
    from ..views import _company_inventory_enabled
    choices = []
    for other in CompanyConfigRecord.objects.filter(is_active=True).order_by("display_name"):
        allowed = available_tabs(other, request.user, _company_inventory_enabled(other))
        choices.append(dict(name=other.display_name, key=other.company_key, tabs=allowed,
            url=reverse("epos_qbo:company-detail", args=[other.company_key]) + "?" + urlencode({"tab": allowed[0]})))
    return choices


def page_context(company, request, inventory_enabled=False, token_health=None):
    key = company.company_key
    tabs = available_tabs(company, request.user, inventory_enabled)
    active = request.GET.get("tab", "overview" if key == ops.COMPANY_KEY else "sales")
    if active not in tabs:
        active = tabs[0]
    base = reverse("epos_qbo:company-detail", args=[key])
    choices = switcher_choices(request)
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
    trend_rows, trend_ticks = [], []
    trend_total = Decimal(0)
    if key == ops.COMPANY_KEY:
        recent_days = [row for row in all_history if row.get("day")][:7]
        max_value = max((row["amount"] for row in recent_days if row.get("confirmed") and row.get("amount") is not None), default=Decimal(0))
        # Four equal intervals, rounded up to a readable step. Zero stays zero.
        unit = Decimal(500000) if max_value >= 1000000 else Decimal(1000) if max_value >= 1000 else Decimal(1)
        step = max(unit, (max_value / 4 / unit).to_integral_value(rounding=ROUND_CEILING) * unit)
        ceiling = step * 4
        def compact(value):
            if value >= 1000000:
                return "₦" + format(value / 1000000, ".2f").rstrip("0").rstrip(".") + "m"
            if value >= 1000:
                return "₦" + format(value / 1000, ".1f").rstrip("0").rstrip(".") + "k"
            return experience.money_label(value)
        trend_ticks = [compact(step * index) for index in (4, 3, 2, 1, 0)]
        for row in reversed(recent_days):
            amount = row.get("amount") if row.get("confirmed") else None
            if amount is not None:
                trend_total += amount
            trend_rows.append(dict(day=row["day"], amount=row["amount_label"] if amount is not None else "Not confirmed",
                compact=compact(amount) if amount is not None else "—",
                height=0 if not amount else float(max(Decimal(0), amount) / ceiling * 100), confirmed=amount is not None))
    if not any(point["confirmed"] for point in trend_rows):
        trend_ticks = []
    register_context.update(company_queue_page=queue_page, company_queue_count=len(decisions),
        company_queue_query=queue_query, company_queue_filter=queue_filter, company_sales_query=sales_query,
        company_sales_filter=sales_filter, company_sales_count=len(history), company_nora_schedules=nora_schedules,
        company_nora_runs=nora_run_rows, company_all_decisions=all_decisions,
        company_decision_counts=decision_counts,
        nora_categories=[dict(kind=kind, label=label, count=decision_counts[kind])
                         for kind, label in category_labels.items() if decision_counts[kind]],
        nora_sales_trend=trend_rows, nora_trend_ticks=trend_ticks, nora_trend_total=experience.money_label(trend_total) if any(point["confirmed"] for point in trend_rows) else "Not confirmed",
        nora_workspace=(key == ops.COMPANY_KEY or active == "sales"), nora_messages_inline=True)
    return dict(**register_context, company_position=position, company_tabs=[dict(key=t, label=TAB_LABELS[t], url=tab_url(base, t, key)) for t in tabs],
        company_key=key, company_tab=active, company_tab_label=TAB_LABELS[active], company_choices=choices,
        company_sales_page=sales_page, company_decisions=decisions, company_record_errors=errors,
        company_history_url=reverse("epos_qbo:runs") + "?" + urlencode({"company":key}),
        company_tab_query=urlencode({"tab":active}))


def shell_context(request, active="attention"):
    """The Nora Mart sidebar/topbar for pages outside the company workspace view (Review queue, confirmation)."""
    from ..views import _company_inventory_enabled
    company = CompanyConfigRecord.objects.filter(company_key=ops.COMPANY_KEY).first()
    if company is None:
        return {}
    tabs = available_tabs(company, request.user, _company_inventory_enabled(company))
    base = reverse("epos_qbo:company-detail", args=[ops.COMPANY_KEY])
    choices = switcher_choices(request)
    try:
        position = next(iter(experience.home_context(ops.COMPANY_KEY)["home_rows"]), None)
    except Exception:  # the shell must never break the page it wraps
        position = None
    return dict(nora_workspace=True, company_key=ops.COMPANY_KEY, company_data={"display_name": company.display_name},
                company_choices=choices, company_position=position or {}, company_tab=active,
                company_tab_label=TAB_LABELS.get(active, "Review queue"),
                company_tabs=[dict(key=t, label=TAB_LABELS[t], url=tab_url(base, t, ops.COMPANY_KEY)) for t in tabs])
