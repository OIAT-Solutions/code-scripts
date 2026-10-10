"""Company tabs over existing records; no remote calls, edits or posting tools."""
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.urls import reverse

from ..models import CompanyConfigRecord
from . import attention, company_a_ops as ops, experience

TAB_LABELS = {"sales": "Sales", "purchases": "Purchases", "products": "Products & Stock",
              "deposits": "Banking", "settings": "Settings"}
STEP_TABS = {"purchases": {"bills"}, "products": {"catalogue", "guard"}, "deposits": {"uf", "deposits", "uf_deposits"}}
KIND_TABS = {"purchases": {"bill", "vendor"}, "products": {"product", "stock_adjust"}, "deposits": {"deposit"}}


def available_tabs(company, user, inventory_enabled=False):
    tabs = ["sales"]
    if company.company_key == ops.COMPANY_KEY:
        tabs += ["purchases", "products", "deposits"]
    if user.has_perm("epos_qbo.can_edit_companies"):
        tabs += ["settings"]
    return tabs


def page_context(company, request, inventory_enabled=False, token_health=None):
    key = company.company_key
    tabs = available_tabs(company, request.user, inventory_enabled)
    active = request.GET.get("tab", "sales")
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
    history = experience.daily_rows(key, include_previews=False) if active == "sales" else []
    sales_page = Paginator(history, 15).get_page(request.GET.get("page"))
    decisions, errors = attention.inbox() if key == ops.COMPANY_KEY and active in STEP_TABS else ([], [])
    decisions = [dict(i, day=experience.date_value(i["date"])) for i in decisions if i["kind"] in KIND_TABS.get(active, set())]
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
    return dict(**register_context, company_position=position, company_tabs=[dict(key=t, label=TAB_LABELS[t], url=base + "?" + urlencode({"tab":t})) for t in tabs],
        company_tab=active, company_tab_label=TAB_LABELS[active], company_choices=choices,
        company_sales_page=sales_page, company_decisions=decisions, company_record_errors=errors,
        company_history_url=reverse("epos_qbo:runs") + "?" + urlencode({"company":key}),
        company_tab_query=urlencode({"tab":active}))
