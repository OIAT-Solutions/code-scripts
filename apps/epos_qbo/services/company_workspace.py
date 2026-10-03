"""Company tabs over existing records; no remote calls, edits or posting tools."""
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.urls import reverse

from ..models import CompanyConfigRecord
from . import attention, company_a_ops as ops, experience, messages

TAB_LABELS = {"sales": "Sales", "purchases": "Purchases", "products": "Products & Stock",
              "suppliers": "Suppliers", "deposits": "Deposits", "settings": "Settings"}
STEP_TABS = {"purchases": {"bills"}, "products": {"catalogue", "guard"},
             "suppliers": {"bills"}, "deposits": {"uf", "deposits", "uf_deposits"}}
KIND_TABS = {"purchases": {"bill"}, "products": {"catalogue"}, "suppliers": {"vendor"}, "deposits": {"deposit"}}


def available_tabs(company, user, inventory_enabled=False):
    tabs = ["sales"]
    if company.company_key == ops.COMPANY_KEY:
        tabs += ["purchases", "products", "suppliers", "deposits"]
    elif inventory_enabled:
        tabs += ["products"]
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
    activity = []
    if key == ops.COMPANY_KEY and active in STEP_TABS:
        for run in ops.list_runs(limit=25):
            for step in run.steps:
                if step.name not in STEP_TABS[active]:
                    continue
                if active == "suppliers" and not {"vendors_created", "vendors_held"}.intersection(step.counts):
                    continue  # A bill hold does not establish a supplier problem.
                facts = []
                # Only fields actually present may be shown, never parser defaults.
                fields = {
                    "purchases": [("posted", "Bills posted"), ("already_posted", "Already posted"), ("hold", "Bills needing review")],
                    "products": [("items_created", "Products created"), ("new_products", "New products checked"), ("alert", "Stock problems"), ("warn", "Stock warnings")],
                    "suppliers": [("vendors_created", "Suppliers created"), ("vendors_held", "Suppliers needing review")],
                    "deposits": [("deposited", "Days deposited"), ("held", "Days needing review"), ("missing", "Days missing a till sheet")],
                }[active]
                for field, label in fields:
                    if field in step.counts and not run.dry_run:
                        facts.append((label, step.counts[field]))
                outcome = messages.step_outcome(step, run.dry_run)
                if active == "suppliers" and not run.dry_run:
                    held = experience.money(step.counts.get("vendors_held"))
                    outcome = dict(label="Suppliers need review" if held is not None and held > 0 else "Supplier results recorded",
                        sentence="These supplier results were recorded during the purchase check.",
                        tone="warning" if held is not None and held > 0 else "neutral")
                activity.append(dict(day=experience.date_value(run.business_date), title="Stock checks" if step.name == "guard" else TAB_LABELS[active],
                    preview=run.dry_run, facts=facts, **outcome,
                    url=reverse("epos_qbo:company-a-run-detail", args=[run.business_date, run.run_id]) + "#step-" + step.name))
    return dict(company_position=position, company_tabs=[dict(key=t, label=TAB_LABELS[t], url=base + "?" + urlencode({"tab":t})) for t in tabs],
        company_tab=active, company_tab_label=TAB_LABELS[active], company_choices=choices,
        company_sales_page=sales_page, company_decisions=decisions, company_record_errors=errors,
        company_step_activity=activity, company_history_url=reverse("epos_qbo:runs") + "?" + urlencode({"company":key}),
        company_tab_query=urlencode({"tab":active}), company_records_limit=25)
