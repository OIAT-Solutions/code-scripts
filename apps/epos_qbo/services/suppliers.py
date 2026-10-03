"""Suppliers tab for Company A: vendors.csv mapping, suppliers waiting for a decision, and the
don't-ask-again list. Local files only; changes go through the inbox confirmation flow."""
from __future__ import annotations

from urllib.parse import urlencode

from django.core.paginator import Paginator

from . import company_a_ops as ops, workspace_records as records

PAGE_SIZE = 50


def vendors_path():
    return ops.state_root() / "mappings" / ops.COMPANY_KEY / "vendors.csv"


def mapping_rows():
    path = vendors_path()
    if not path.exists():
        return []
    out = []
    for row in records.rows(path):
        out.append(dict(epos_id=row.get("EPOS Supplier Id", ""), epos_name=row.get("EPOS Supplier Name", ""),
                        qbo_id=row.get("QBO Vendor Id", ""), qbo_name=row.get("QBO Vendor Name", ""),
                        approved_by=row.get("Approved By", ""), approved=bool(row.get("Approved By") and row.get("QBO Vendor Id"))))
    return sorted(out, key=lambda r: r["epos_name"].casefold())


def context(request):
    from . import attention, exclusions
    errors = []
    try:
        mapped = mapping_rows()
    except (OSError, ValueError):
        mapped = []
        errors.append("The supplier list (vendors.csv) could not be read.")
    query = request.GET.get("q", "").strip()[:200]
    if query:
        needle = query.casefold()
        mapped_shown = [r for r in mapped if needle in " ".join([r["epos_name"], r["qbo_name"], r["epos_id"], r["qbo_id"]]).casefold()]
    else:
        mapped_shown = mapped
    items, inbox_errors = attention.inbox()
    held = [i for i in items if i["kind"] == "vendor"]
    try:
        excluded = exclusions.rows(include_expired=True)
    except (OSError, ValueError):
        excluded = []
        errors.append("The don't-ask-again list could not be read.")
    return dict(supplier_page=Paginator(mapped_shown, PAGE_SIZE).get_page(request.GET.get("page")), supplier_total=len(mapped),
                supplier_query=query, supplier_query_params=urlencode({"tab": "suppliers", "q": query}),
                supplier_held=held, supplier_exclusions=excluded, supplier_errors=errors + [e for e in inbox_errors if "supplier" in e.lower()])
