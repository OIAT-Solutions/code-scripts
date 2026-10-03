"""Presenter for Claude's stock_snapshot schema v1. Never recomputes stock."""
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from django.core.paginator import Paginator
from . import workspace_records as records

STATUS = {
 "NEGATIVE_QBO":("Negative in QuickBooks","danger"),"NEGATIVE_EPOS":("Negative in EPOS","danger"),
 "NO_QBO_ITEM":("QuickBooks item missing","danger"),"DIFFERENT":("Stock differs","warning"),
 "NOT_IN_EPOS_REPORT":("EPOS stock not found","warning"),"NOT_TRACKED_IN_EPOS":("No stock comparison","neutral"),
 "MATCH":("Stock agrees","success"),"UNMAPPED":("New in EPOS · not set up","warning")}
ORDER={key:i for i,key in enumerate(STATUS)}


def context(request):
    data,error={},""
    path=records.ops.ops_root()/"stock_snapshot/latest.json"
    try:
        data=records.document(path)
        if data.get("schema_version")!=1 or data.get("company")!="company_a" or not isinstance(data.get("rows"),list) or not isinstance(data.get("unmapped_epos_products"),list):
            raise ValueError("Unsupported snapshot")
    except FileNotFoundError:pass
    except (ValueError,OSError,TypeError):
        data={};error="The stock snapshot could not be read. Update stock or check the last update details."
    rows=[]
    for raw in data.get("rows",[]):
        if not isinstance(raw,dict): error="Some stock records could not be read.";continue
        status=raw.get("status","")
        label,tone=STATUS.get(status,("Needs checking","neutral"))
        if status=="DIFFERENT" and raw.get("likely_timing") is True:label="Different · timing may explain it"
        rows.append(dict(raw, id="qbo-"+str(raw.get("qbo_item_id") or raw.get("family_sku")),
            name=raw.get("qbo_name") or raw.get("epos_master_name") or "Unnamed product",label=label,tone=tone,
            epos_display=records.quantity(raw.get("epos_qty_canonical")),qbo_display=records.quantity(raw.get("qbo_qty_on_hand")),
            difference_display=records.quantity(raw.get("difference")) if raw.get("type")=="Inventory" else "Not compared"))
    for raw in data.get("unmapped_epos_products",[]):
        if not isinstance(raw,dict):error="Some new product records could not be read.";continue
        rows.append(dict(raw,id="epos-"+str(raw.get("epos_product_id")),status="UNMAPPED",name=raw.get("name") or "Unnamed product",
            label=STATUS["UNMAPPED"][0],tone="warning",epos_display=records.quantity(raw.get("epos_qty")),
            qbo_display="Not set up",difference_display="Not compared",epos_product_ids=[raw.get("epos_product_id")],
            canonical_unit="EPOS units",family_sku="",qbo_name="",flags=[]))
    query=request.GET.get("q","").strip()[:200];status=request.GET.get("status","all")
    if status not in {"all","negative","unmapped","different","match","review"}:status="all"
    selected=next((r for r in rows if r["id"]==request.GET.get("product")),None)
    def accepts(row):
        key=row.get("status")
        return status=="all" or (status=="negative" and key in {"NEGATIVE_QBO","NEGATIVE_EPOS"}) or (status=="unmapped" and key=="UNMAPPED") or (status=="different" and key=="DIFFERENT") or (status=="match" and key=="MATCH") or (status=="review" and key not in {"MATCH","NOT_TRACKED_IN_EPOS"})
    filtered=[r for r in rows if accepts(r) and (not query or query.casefold() in " ".join(str(r.get(k) or "") for k in ("name","family_sku","qbo_item_id","epos_product_ids","epos_master_name","category")).casefold())]
    filtered.sort(key=lambda r:(ORDER.get(r.get("status"),99),r["name"].casefold()))
    sources=data.get("sources") or {};epos=records.timestamp((sources.get("epos_stock_report") or {}).get("at"));qbo=records.timestamp((sources.get("qbo") or {}).get("read_at"))
    from ..models import RunJob
    latest=RunJob.objects.filter(scope=RunJob.SCOPE_WORKSPACE_READ,company_key="company_a",inventory_options_json__action="stock").order_by("-created_at").first()
    return dict(stock_page=Paginator(filtered,25).get_page(request.GET.get("page")),stock_selected=selected,
        stock_total=len(rows),stock_unmapped=sum(r.get("status")=="UNMAPPED" for r in rows),
        stock_negative=sum(r.get("status") in {"NEGATIVE_EPOS","NEGATIVE_QBO"} for r in rows),
        stock_different=sum(r.get("status")=="DIFFERENT" for r in rows),stock_query=query,stock_filter=status,
        stock_query_params=urlencode({"tab":"products","q":query,"status":status}),stock_error=error,stock_available=bool(data),
        stock_generated=records.timestamp(data.get("generated_at")),stock_epos_at=epos,stock_qbo_at=qbo,
        stock_stale=any(datetime.now(timezone.utc)-at>timedelta(hours=24) for at in (epos,qbo) if at),
        stock_note=(data.get("business_context") or {}).get("note",""),stock_summary=data.get("summary_text",""),
        stock_support={k:data.get(k) for k in ("summary","unassigned_stock_rows","qbo_akp_items_not_in_mapping","timing_evidence")},stock_job=latest)
