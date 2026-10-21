"""Products & Stock for Company A: a presenter over the stock snapshot (schema v1).

The snapshot (``STATE_ROOT/ops/company_a/stock_snapshot/latest.json``) is produced by
``code_scripts.akponora_ops.stock_snapshot``; this module never recomputes stock. It adds the
installed mapping (who approved each link) and the catalogue snapshot (EPOS category) for search,
and links new EPOS products to their inbox decisions. Local files only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.urls import reverse

from . import workspace_records as records

STOCK = {
    "NEGATIVE_QBO": ("Negative in QuickBooks", "danger"),
    "NEGATIVE_EPOS": ("Negative in EPOS", "danger"),
    "NO_QBO_ITEM": ("QuickBooks item missing", "danger"),
    "DIFFERENT": ("Different", "warning"),
    "NOT_IN_EPOS_REPORT": ("Not in EPOS stock report", "warning"),
    "UNMAPPED": ("Not mapped", "warning"),
    "NOT_TRACKED_IN_EPOS": ("Not compared", "neutral"),
    "MATCH": ("Same", "success"),
}
ORDER = {key: i for i, key in enumerate(STOCK)}
LINK = {"linked": ("Linked in QuickBooks", "success"), "check": ("Link needs checking", "warning"),
        "unmapped": ("Not mapped", "warning"), "excluded": ("Not mapped · don't ask again", "neutral")}
CHECK_FLAGS = {
    "QBO_ITEM_INACTIVE": "The QuickBooks item is inactive.",
    "MASTER_NOT_IN_CATALOGUE": "The EPOS main product is no longer in the EPOS catalogue.",
    "AMBIGUOUS_EPOS_NAME": "Two EPOS products share this name, so the stock report row can't be matched with certainty.",
    "NONINVENTORY_BUT_EPOS_TRACKED": "EPOS tracks stock for it, but QuickBooks has it as a non-stock item.",
}
FILTERS = {
    "all": "All products", "attention": "Needs attention", "negative_qbo": "Negative in QuickBooks",
    "different": "Different", "unmapped": "Not mapped", "check": "Link needs checking", "match": "Same",
}
PAGE_SIZE = 50


def snapshot_path():
    return records.ops.ops_root() / "stock_snapshot" / "latest.json"


def load_snapshot():
    """(data, error). A missing file is not an error: the page shows a calm empty state."""
    path = snapshot_path()
    if not path.exists():
        return {}, ""
    try:
        data = records.document(path)
        if data.get("schema_version") != 1 or data.get("company") != "company_a" \
                or not isinstance(data.get("rows"), list) or not isinstance(data.get("unmapped_epos_products"), list):
            raise ValueError("Unsupported snapshot")
        return data, ""
    except (OSError, ValueError, TypeError):
        return {}, "The saved stock check could not be read. Update products & stock to make a new one."


def unmapped_ids():
    data, _ = load_snapshot()
    return {str(r.get("epos_product_id")) for r in data.get("unmapped_epos_products", []) if isinstance(r, dict) and r.get("epos_product_id")}


def unmapped_name(pid):
    data, _ = load_snapshot()
    row = next((r for r in data.get("unmapped_epos_products", []) if isinstance(r, dict) and str(r.get("epos_product_id")) == str(pid)), {})
    return row.get("name") or f"EPOS product {pid}"


def mapping_by_pid():
    path = records.ops.state_root() / "mappings/company_a/approved.csv"
    if not path.exists():
        return {}
    return {str(r.get("EPOS Product ID", "")).strip(): r for r in records.rows(path)}


def catalogue_categories():
    path = records.ops.ops_root() / "catalogue_sync" / "catalogue_snapshot.json"
    if not path.exists():
        return {}
    data = records.document(path)
    return {str(pid): (p or {}).get("CategoryName") or "" for pid, p in data.items() if isinstance(p, dict)}


def quantity(value, unit=""):
    n = records.number(value)
    if n is None:
        return "Not known"
    text = format(n.normalize(), ",f")
    return f"{text} {unit}".strip()


def link_state(raw, mapping):
    """Is this QuickBooks link trustworthy? Plain reasons; technical flags stay in Details."""
    reasons = []
    if raw.get("status") == "NO_QBO_ITEM" or not raw.get("qbo_item_id"):
        reasons.append("The mapping points to a QuickBooks item that does not exist.")
    if raw.get("qbo_active") is False:
        reasons.append(CHECK_FLAGS["QBO_ITEM_INACTIVE"])
    for flag in raw.get("flags") or []:
        text = CHECK_FLAGS.get(str(flag).split("(")[0])
        if text and text not in reasons:
            reasons.append(text)
    pids = [str(p) for p in raw.get("epos_product_ids") or []]
    rows = [mapping[p] for p in pids if p in mapping]
    if mapping and rows and any((r.get("Review Status") or "").strip().lower() != "approved" for r in rows):
        reasons.append("The mapping row is not approved yet.")
    approved_by = next((r.get("Approved By") for r in rows if r.get("Approved By")), "")
    return ("check" if reasons else "linked"), reasons, approved_by


def build_rows(data, mapping, categories, decisions, excluded):
    out = []
    for raw in data.get("rows", []):
        if not isinstance(raw, dict):
            continue
        unit = raw.get("canonical_unit") or ""
        status = raw.get("status") or ""
        label, tone = STOCK.get(status, ("Needs checking", "neutral"))
        link, link_reasons, approved_by = link_state(raw, mapping)
        master = str(raw.get("epos_master_id") or "")
        compared = raw.get("type") == "Inventory" and status not in {"NOT_TRACKED_IN_EPOS", "NO_QBO_ITEM"}
        out.append(dict(
            id="qbo-" + str(raw.get("qbo_item_id") or raw.get("family_sku")), status=status,
            name=raw.get("qbo_name") or raw.get("epos_master_name") or "Unnamed product",
            epos_name=raw.get("epos_master_name") or "", sku=raw.get("family_sku") or "",
            qbo_item_id=raw.get("qbo_item_id") or "", type=raw.get("type") or "", unit=unit,
            category=categories.get(master, ""), epos_ids=[str(p) for p in raw.get("epos_product_ids") or []],
            stock_label=label, stock_tone=tone, link=link, link_label=LINK[link][0], link_tone=LINK[link][1],
            link_reasons=link_reasons, approved_by=approved_by,
            epos=quantity(raw.get("epos_qty_canonical"), unit) if compared or raw.get("epos_qty_canonical") is not None else "Not tracked",
            qbo=quantity(raw.get("qbo_qty_on_hand"), unit) if raw.get("type") == "Inventory" else "Not a stock item",
            difference=quantity(raw.get("difference"), unit) if compared and status != "MATCH" and raw.get("difference") is not None else "",
            likely_timing=raw.get("likely_timing") is True, timing_reasons=raw.get("timing_reasons") or [],
            flags=raw.get("flags") or [], raw=raw, decision=None))
    for raw in data.get("unmapped_epos_products", []):
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("epos_product_id") or "")
        link = "excluded" if pid in excluded else "unmapped"
        out.append(dict(
            id="epos-" + pid, status="UNMAPPED", name=raw.get("name") or f"EPOS product {pid}", epos_name=raw.get("name") or "",
            sku="", qbo_item_id="", type="", unit="", category=raw.get("category") or categories.get(pid, ""), epos_ids=[pid],
            stock_label=STOCK["UNMAPPED"][0], stock_tone="neutral" if link == "excluded" else "warning",
            link=link, link_label=LINK[link][0], link_tone=LINK[link][1], link_reasons=[], approved_by="",
            epos=quantity(raw.get("epos_qty")) if raw.get("tracked") else "Not tracked", qbo="Not set up", difference="",
            likely_timing=False, timing_reasons=[], flags=[], raw=raw, pid=pid, tracked=bool(raw.get("tracked")),
            decision=decisions.get(pid)))
    return out


def accepts(row, wanted):
    if wanted == "all":
        return True
    if wanted == "attention":
        return row["status"] in {"NEGATIVE_QBO", "NEGATIVE_EPOS", "NO_QBO_ITEM", "DIFFERENT", "NOT_IN_EPOS_REPORT"} \
            or row["link"] in {"check", "unmapped"}
    if wanted == "negative_qbo":
        return row["status"] == "NEGATIVE_QBO"
    if wanted == "different":
        return row["status"] == "DIFFERENT"
    if wanted == "unmapped":
        return row["link"] in {"unmapped", "excluded"}
    if wanted == "check":
        return row["link"] == "check"
    if wanted == "match":
        return row["status"] == "MATCH"
    return True


def matches(row, query):
    if not query:
        return True
    haystack = " ".join([row["name"], row["epos_name"], row["sku"], str(row["qbo_item_id"]), row["category"], " ".join(row["epos_ids"])])
    return query.casefold() in haystack.casefold()


def latest_job():
    from ..models import RunJob
    return RunJob.objects.filter(scope=RunJob.SCOPE_WORKSPACE_READ, company_key="company_a",
                                 inventory_options_json__action="stock").order_by("-created_at").first()


def context(request):
    data, error = load_snapshot()
    errors = [error] if error else []
    try:
        mapping = mapping_by_pid()
    except (OSError, ValueError):
        mapping = {}
        errors.append("The installed product mapping could not be read; link checks may be incomplete.")
    try:
        categories = catalogue_categories()
    except (OSError, ValueError):
        categories = {}
    from . import attention, exclusions
    items, _ = attention.inbox()
    decisions = {i["identity"]: i for i in items if i["kind"] == "product"}
    excluded = exclusions.active_keys("product")
    rows = build_rows(data, mapping, categories, decisions, excluded)
    query = request.GET.get("q", "").strip()[:200]
    wanted = request.GET.get("status", "all")
    if wanted not in FILTERS:
        wanted = "all"
    filtered = [r for r in rows if accepts(r, wanted) and matches(r, query)]
    filtered.sort(key=lambda r: (ORDER.get(r["status"], 99), r["name"].casefold()))
    selected = next((r for r in rows if r["id"] == request.GET.get("product")), None)
    sources = data.get("sources") or {}
    epos_at = records.timestamp((sources.get("epos_stock_report") or {}).get("at"))
    qbo_at = records.timestamp((sources.get("qbo") or {}).get("read_at"))
    generated = records.timestamp(data.get("generated_at"))
    job = latest_job()
    counts = {key: sum(accepts(r, key) for r in rows) for key in FILTERS}
    summary = data.get("summary") or {}
    business = data.get("business_context") or {}
    return dict(
        stock_page=Paginator(filtered, PAGE_SIZE).get_page(request.GET.get("page")), stock_selected=selected,
        stock_counts=counts, stock_filters=[(k, v, counts[k]) for k, v in FILTERS.items()], stock_filter=wanted,
        stock_query=query, stock_query_params=urlencode({"tab": "products", "q": query, "status": wanted}),
        stock_errors=errors, stock_available=bool(data), stock_generated=generated, stock_epos_at=epos_at, stock_qbo_at=qbo_at,
        stock_stale=bool(generated and datetime.now(timezone.utc) - generated > timedelta(hours=26)),
        stock_note=business.get("note", ""), stock_summary=data.get("summary_text", ""),
        stock_unposted=business.get("unposted_sales_days") or [], stock_last_posted=business.get("last_posted_sales_date"),
        stock_support={k: data.get(k) for k in ("unassigned_stock_rows", "qbo_akp_items_not_in_mapping")},
        stock_unassigned=len(data.get("unassigned_stock_rows") or []), stock_qbo_extra=len(data.get("qbo_akp_items_not_in_mapping") or []),
        stock_rows_total=summary.get("rows", len(data.get("rows", []))), stock_job=job,
        stock_job_running=bool(job and job.status in {"queued", "running"}),
        stock_inbox_url=reverse("epos_qbo:attention"))
