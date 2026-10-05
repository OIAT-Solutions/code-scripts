"""Received EPOS purchase orders -> reviewable QBO Bills on the October AKP- items (company_a).

Realm 9341455406194328 (production). AGENTS.md governs: Bills only, on the new ``AKP-`` /
``AKP-NS-`` items, never LEGACY items, the catch-all 15030 or the 120xxx accounts. Bills are
left UNPAID (the owner pays them in QBO); this tool never creates BillPayments, items or accounts,
never posts InventoryAdjustment and never edits or voids an existing bill. It creates a QBO vendor
only in ``scheduled`` mode, for a genuinely new EPOS supplier, behind its own env gate and cap
(see ``code_scripts/akponora_ops/vendors.py``). The EPOS PO "MODE OF PAYMENT" (CASH / TRANSFER)
goes into the Bill PrivateNote as a payment hint only.

Subcommands
-----------
``plan`` (default; READ-ONLY: EPOS view-only via Playwright, QBO GET only)
    Captures the EPOS POs received in ``--from..--to`` (business date, 05:00 Africa/Lagos
    cutoff; default from = the cursor in ``STATE_ROOT/ops/company_a/bills_sync/cursor.json``,
    floor 2026-10-01; default to = today), resolves every received line through the approved
    mapping and the vendor map, checks QBO for already-posted / manual / duplicate bills and
    writes ``plan.json``, ``review.csv`` (with an empty ``Approve`` column), ``review_lines.csv``,
    ``payloads.jsonl``, ``summary.json`` (sha256 of the payloads) and ``review.xlsx``.
    ``STATE_ROOT/mappings/company_a/review_exclusions.csv`` (next to vendors.csv): a ``bill``
    exclusion makes that PO ``EXCLUDED`` (resolved outside the tool, permanently); a ``vendor``
    exclusion HOLDs the supplier's bills with reason ``supplier excluded`` and the supplier is never
    auto-created. The file path + sha256 go into summary.json ``exclusions_file``.
``post`` (WRITES; needs a chat yes)
    Posts the READY bills marked ``Approve=yes`` in ``--review`` (``skip`` / ``resolved`` = resolved
    by a human outside this tool for THIS plan: never posted, counts as done for the cursor). Review
    exclusions (``review_exclusions``, read from the file the plan recorded) win over Approve=yes:
    an excluded PO is recorded ``RESOLVED`` and an excluded supplier ``HELD_LIVE``; neither posts.
    Needs ``--approval-ref`` and ``--expect-sha``. Before each POST the
    DocNumber, manual near-duplicates, the vendor and every item are re-checked live; each Bill
    is re-read and verified; ``results.csv`` makes it resumable; the cursor advances only over
    business days whose POs are all done. ``--auto`` (no Approve column) only with
    ``OIAT_COMPANY_A_BILLS_AUTO_POST=1`` and ``OIAT_COMPANY_A_BILLS_APPROVAL_REF``; per-bill cap
    ``OIAT_COMPANY_A_BILLS_AUTO_MAX_BILL`` (default 2,000,000) and per-run count cap
    ``OIAT_COMPANY_A_BILLS_AUTO_MAX_COUNT`` (default 20). Off by default.
``scheduled``
    ``plan`` with the default window, then ``post --auto`` when the auto gates are on. Exit 0 =
    nothing waiting, 3 = bills wait for human review (READY or HOLD), 2 = stopped. Before the
    bills are planned, unmapped suppliers are scored against live QBO vendors: genuinely new ones
    (best score < 0.75) are created when ``OIAT_COMPANY_A_VENDOR_AUTO_CREATE=1`` and
    ``OIAT_COMPANY_A_VENDOR_APPROVAL_REF`` are set (cap ``OIAT_COMPANY_A_VENDOR_AUTO_MAX``, default
    5) and appended to vendors.csv as ``auto:<ref>``; near matches HOLD with the candidates.
``vendors-suggest`` (READ-ONLY)
    Fuzzy-matches every EPOS supplier name (current POs plus the September PO-to-bill matches in
    ``--history``) against live QBO vendors and writes ``vendors_suggest.csv`` for a human. The
    approved rows are copied by a human into ``STATE_ROOT/mappings/company_a/vendors.csv``
    (EPOS Supplier Id, EPOS Supplier Name, QBO Vendor Id, QBO Vendor Name, Approved By).

Empirical rules (evidence: ``outputs/grni_2026-09/evidence/`` 266 September POs, 749 lines;
``outputs/close_2026-09-30/pos_oct/`` 1 Oct POs; ``outputs/epos_master_links_2026-10-01/proof_1oct.py``)
-----------------------------------------------------------------------------------------------
* Quantity. PO detail ``VolumeOfSale``, ``CostPriceMeasurementUnitVolume`` and ``Factor`` are
  null on every captured line (749/749 Sep, 3/3 Oct). ``QuantityReceived`` is in the unit of
  the PO's own EPOS product. 748/749 Sep lines are on the tracked family master
  (``Target QBO SKU == AKP-{ProductId}``), whose mapping purchase multiplier equals the
  catalogue ``VolumeOfSale`` (400 lines) or 1 when it has none (348). EPOS adds
  ``QuantityReceived`` master units to stock, so canonical QBO qty = ``QuantityReceived x Staff
  Approved Purchase Multiplier``; ``proof_1oct`` reconciles 1 Oct stock history with exactly
  this rule (496/496 sold families). And ``ValueExcTax / multiplier`` equals the W7 per-unit
  PurchaseCost (catalogue CostPriceExTax / owner multiplier) on 748/749 lines. The one exception
  (PO 3873, RAMBO 300ml, an untracked child of another family) has no proven stock effect.
  HOLD rules: a line on a non-master product, a line with Factor / VolumeOfSale set, or a unit
  cost outside 0.5x-2x of the item's QBO PurchaseCost (the signature of a wrong multiplier).
* Value. ``ValueExcTax`` / ``ValueIncTax`` are per PO unit, not per line: QuantityReceived x
  unit reproduces the list ``TotalValueReceivedExTax`` / ``TotalValueReceived`` on 265/266 POs
  (``ActualProductCostPrice`` differs on 9 lines, so it is not used). A partial receipt is
  therefore prorated by construction (line = QuantityReceived x unit). A PO whose line sum
  differs from the list total by more than N1 (ex or inc) is held.
* Tax and structure. All 169 QBO Bills of 15 Aug-1 Oct: APAccountRef 1150040014, DepartmentRef 2,
  blank DocNumber, DueDate = TxnDate (15 carry SalesTermRef 1), GlobalTaxCalculation
  TaxExcluded (167/169) with line TaxCodeRef 7 "No VAT" (438/442 lines), and the line amount is
  the supplier's gross = the EPOS inc-tax cost (e.g. PO 3704 = Bill 74483 = 520,000). Default
  ``--tax-mode gross`` copies that: lines at QuantityReceived x ValueIncTax with TaxCode 7, so
  the Bill total = EPOS TotalValueReceived. ``--tax-mode split`` (accountant decision) books
  lines ex-tax with TaxCode 2 (7.5% S) on VAT lines / 7 on 0% lines; the total is the same.
* DocNumber ``EPOS-PO-{OrderRef}`` (<= 21 chars) is deterministic; manual bills never use it.
* September receipts (business date < 2026-10-01) are already accrued to GRNI 210200 (Id 87):
  they are EXCLUDED and never become bills.

Run from the repo root:
  .venv/bin/python -m code_scripts.akponora_ops.bills_sync plan --out outputs/bills_sync_<date>
  .venv/bin/python -m code_scripts.akponora_ops.bills_sync post --review <dir>/review.csv \
      --approval-ref "<chat yes>" --expect-sha <summary.json payloads_sha256>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from playwright.sync_api import sync_playwright

from code_scripts.akponora_ops import review_exclusions
from code_scripts.akponora_ops import vendors as vendor_ops
from code_scripts.akponora_ops.common import (
    AKP_NS_SKU_PREFIX, AKP_SKU_PREFIX, ASSET_ID, CATCH_ALL_ITEM_ID, COMPANY, INV_START, LEGACY_PREFIX, REALM,
    dump_json, env_flag, mapping_file, read_csv, run_dir, send_slack, sha256_file, state_dir, write_csv,
)
from code_scripts.product_conversion import (
    ProductConversionRegistry, ProductResolutionError, canonical_product_id, clean, october_target_error,
)
from code_scripts.scripts.akponora_cutover._common import (
    REPO_ROOT, business_date, company_config, epos_login, setup_env,
)
from code_scripts.scripts.akponora_cutover.bills_from_epos_pos import (
    PO_LIST_URL, extract_po_model, norm,
)
from code_scripts.scripts.akponora_cutover.w7_create_items import (
    D, QBOClient, StopRun, canonical_json, now_iso, q, qbo_escape, sha256_text,
)

TOOL = "bills_sync"
DOC_PREFIX = "EPOS-PO-"
AP_ACCOUNT_ID = "1150040014"  # 200000 - Accounts Payable, on every 2026 bill
DEPARTMENT_ID = "2"  # Plot C, Golf Road
TAX_NO_VAT = "7"
TAX_STANDARD = "2"
VAT_RATE = Decimal("0.075")
EXPENSE_IDS = {"74", "1150040020", "1150040033", "1150040034"}  # 200100/200300/200201/200202
SEPTEMBER_NOTE = "September \u2014 bookkeeper clears against 210200 (GRNI, QBO Id 87)"
RECEIVED_STATUSES = {"received", "partially received", "partiallyreceived", "part received"}
TZ = ZoneInfo("Africa/Lagos")
TOL = Decimal("1.00")
AUTO_ENV = "OIAT_COMPANY_A_BILLS_AUTO_POST"
AUTO_REF_ENV = "OIAT_COMPANY_A_BILLS_APPROVAL_REF"
AUTO_MAX_BILL_ENV = "OIAT_COMPANY_A_BILLS_AUTO_MAX_BILL"
AUTO_MAX_COUNT_ENV = "OIAT_COMPANY_A_BILLS_AUTO_MAX_COUNT"
APPROVE_YES = {"yes", "y", "true", "1", "approve", "approved"}
APPROVE_SKIP = {"skip", "resolved"}
DONE_RESULTS = {"POSTED", "ADOPTED", "RESOLVED"}
PO_EXCLUDED_NOTE = "PO excluded in review_exclusions - resolved outside this tool"
DEFAULT_HISTORY = REPO_ROOT / "outputs" / "grni_2026-09"

VENDOR_COLS = vendor_ops.VENDOR_COLS
REVIEW_COLS = ["PO", "Received At", "Received Date", "GRN", "EPOS Supplier", "Payment Mode", "QBO Vendor Id",
               "QBO Vendor Name", "Lines", "EPOS Total Ex", "EPOS Total Inc", "Bill Total", "DocNumber", "Status",
               "Reasons", "Warnings", "Payload SHA", "Approve"]
LINE_COLS = ["PO", "Line", "EPOS Product ID", "EPOS Product", "Qty Ordered", "Qty Received", "Multiplier",
             "QBO Item Id", "QBO Item Name", "QBO Sku", "QBO Type", "QBO Qty", "Unit Cost", "Amount", "TaxCode",
             "EPOS Unit Ex", "EPOS Unit Inc", "Tax %", "QBO PurchaseCost", "Cost Ratio", "QBO QtyOnHand",
             "QtyOnHand After", "Line Flags"]
RESULT_COLS = ["ts", "PO", "DocNumber", "status", "Bill Id", "SyncToken", "Total", "payload_sha256", "requestid",
               "approval_ref", "mode", "detail"]
SUGGEST_COLS = ["EPOS Supplier Id", "EPOS Supplier Name", "Normalized", "PO Count", "PO Refs", "Already Mapped",
                "Suggested QBO Vendor Id", "Suggested QBO Vendor Name", "Score", "Evidence", "Second Choice",
                "Approved By"]


# ---------------------------------------------------------------- small helpers
def r2(value: Decimal) -> Decimal:
    return q(value, "0.01")


def money(value) -> str:
    return f"{Decimal(value):.2f}"


def qty_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value == value.to_integral() else str(value)


def doc_number(ref) -> str:
    doc = f"{DOC_PREFIX}{ref}"
    if len(doc) > 21:
        raise StopRun(f"DocNumber {doc} longer than 21 characters")
    return doc


def requestid_for(doc: str, payload_sha: str) -> str:
    return sha256_text(f"{TOOL}|{REALM}|{doc}|{payload_sha}")[:36]


def as_list(rows) -> list:
    if rows is None:
        return []
    return rows if isinstance(rows, list) else [rows]


def vendor_file() -> Path:
    return mapping_file().parent / "vendors.csv"


def ensure_vendor_file(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(path, [], VENDOR_COLS)


def vendor_key(name: str) -> str:
    return norm(name)


_SUPPLIER_RE = re.compile(r"S\s*U+\s*P+\s*L\s*I\s*E\s*R\s*S?\s*[:\-]?", re.I)
_PAYMENT_RE = re.compile(r"MODE\s*OF\s*PAY\w*(?:\s+ME?NT\w*)?\s*[:\-]?", re.I)


def payment_mode(text) -> str:
    """CASH / TRANSFER (or POS / CHEQUE / CREDIT) from free text; '' when absent; else the text."""
    t = " ".join(clean(text).upper().split()).strip(" ,.;:-")
    if not t:
        return ""
    for needle, mode in (("CASH", "CASH"), ("TRANSFER", "TRANSFER"), ("TRANSFR", "TRANSFER"), ("TRF", "TRANSFER"),
                         ("BANK", "TRANSFER"), ("POS", "POS"), ("CHEQUE", "CHEQUE"), ("CREDIT", "CREDIT")):
        if needle in t:
            return mode
    return t[:40]


def payment_detail(note) -> tuple[str, str]:
    """(paid, account) from the text after MODE OF PAYMENT on an EPOS PO note.

    Staff convention (owner, 4 Oct 2026): ``CASH (PAID)``, ``TRANSFER (PAID, MONIEPOINT 4686)``,
    ``CREDIT`` / ``NOT PAID``. paid is ``PAID``, ``NOT PAID`` or '' (not stated); account is the longest
    run of 4+ digits (a bank account number or its last digits) or ''."""
    m = _PAYMENT_RE.search(clean(note))
    text = " ".join(clean(note)[m.end():].upper().split()) if m else ""
    if not text:
        return "", ""
    if re.search(r"NOT\s*PAID|UNPAID|OWING|\bCREDIT\b|\bDEBT\b", text):
        paid = "NOT PAID"
    elif re.search(r"\bPAID\b", text):
        paid = "PAID"
    else:
        paid = ""
    digits = sorted(re.findall(r"\d{4,}", text), key=len)
    return paid, (digits[-1] if digits else "")


def parse_po_note(note) -> tuple[str, str, str]:
    """(supplier, payment mode, note) from an EPOS PO note. Tolerates the staff spellings seen on
    real POs: ``SUPPLIER:X   MODE OF PAYMENT:CASH``, ``SUPPLIER: BUNARICH  BREAD,  MODE OF PAYMENY:
    CASH`` (PO 3968), ``SUUPPLIER: ... MODE OF PAY MENT:TRANSFER``."""
    note = clean(note)
    sup_m, pay_m = _SUPPLIER_RE.search(note), _PAYMENT_RE.search(note)
    supplier, payment = "", ""
    if sup_m:
        end = pay_m.start() if pay_m and pay_m.start() > sup_m.end() else len(note)
        supplier = " ".join(note[sup_m.end():end].split()).strip(" ,.;:-")
    if pay_m:
        payment = payment_mode(note[pay_m.end():])
    return supplier, payment, note


def today_lagos() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def parse_epos_ts(value) -> datetime | None:
    text = clean(value)
    if not text:
        return None
    m = re.search(r"/Date\((\d+)\)/", text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, TZ).replace(tzinfo=None)
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


# ---------------------------------------------------------------- cursor
def cursor_path() -> Path:
    return state_dir(TOOL) / "cursor.json"


def read_cursor() -> dict:
    path = cursor_path()
    return json.loads(path.read_text()) if path.exists() else {}


def default_from() -> str:
    last = read_cursor().get("last_complete_business_date")
    if not last:
        return INV_START
    return max(INV_START, (date.fromisoformat(last) + timedelta(days=1)).isoformat())


# ---------------------------------------------------------------- EPOS (view only)
PO_CACHE = "po_detail_cache.jsonl"


def po_fingerprint(order: dict) -> str:
    """The PO list row as EPOS shows it: any change (received again, edited, re-totalled) changes this."""
    return json.dumps(order, sort_keys=True, default=str)


def load_po_cache(path: Path) -> dict:
    """{OrderRef: {"fingerprint", "detail"}}; the last line for a ref wins. A bad line is skipped."""
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            row = json.loads(line)
            out[str(row["ref"])] = row
        except (ValueError, KeyError, TypeError):
            continue
    return out


def prune_po_cache(path: Path, keep_refs) -> int:
    """Keep only POs still in this run's EPOS list (about 3 weeks), one line each, so the cache never
    grows past one look-back window. Returns the number of POs kept."""
    keep = {str(r) for r in keep_refs}
    rows = [row for ref, row in load_po_cache(path).items() if ref in keep]
    try:
        tmp = Path(path).with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return len(rows)


def cached_details(orders: list, cache: dict, cacheable) -> dict:
    """Details of earlier (look-back) POs whose list row is unchanged since they were opened."""
    out = {}
    for o in orders:
        ref = str(o.get("OrderRef"))
        hit = cache.get(ref)
        if hit and cacheable(o) and hit.get("fingerprint") == po_fingerprint(o):
            out[ref] = hit["detail"]
    return out


def capture_epos(ev: Path, *, order_from: str, order_to: str, want, company: str = COMPANY,
                 cacheable=lambda o: False, cache_path: Path | None = None) -> tuple[list, dict]:
    """View-only EPOS capture. Opens the PO list for an order-date range, then the Details page of
    each order ``want(order)`` selects (rows are matched by the OrderRef in the row text).
    Clicks only login, the date filter + Apply and Details. Resumable via ``po_details.jsonl``."""
    cfg = company_config(company)
    dfrom = datetime.strptime(order_from, "%Y-%m-%d").strftime("%d/%m/%Y")
    dto = datetime.strptime(order_to, "%Y-%m-%d").strftime("%d/%m/%Y")
    lists = []
    details_path = ev / "po_details.jsonl"
    details = {}
    if details_path.exists():
        for line in details_path.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                details[str(d["OrderRef"])] = d

    def on_resp(resp):
        if "GetPurchaseOrders" in resp.url and resp.request.method == "GET":
            try:
                lists.append(json.loads(resp.text()))
            except ValueError:
                pass

    def open_list(page):
        page.goto(PO_LIST_URL)
        page.wait_for_load_state("networkidle")
        for sel, val in (("purchase-order__from-date--input", dfrom), ("purchase-order__to-date--input", dto)):
            inp = page.locator(f'[data-qa-id="{sel}"]')
            inp.click()
            inp.fill(val)
            inp.press("Tab")
        page.keyboard.press("Escape")
        page.locator('[data-qa-id="purchase-order__apply-filter-button"]').click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)

    sel = '[data-qa-id="purchase-order__orders-list__details"]'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1600, "height": 1000}).new_page()
        page.on("response", on_resp)
        epos_login(page, cfg)
        open_list(page)
        if not lists:
            browser.close()
            raise StopRun("EPOS purchase order list did not load")
        body = lists[-1]
        orders = body.get("orders") or []
        dump_json(ev / "po_list_raw.json", {"order_from": order_from, "order_to": order_to, "body": body})
        cached = {}
        if cache_path is not None:
            cached = {ref: d for ref, d in cached_details(orders, load_po_cache(cache_path), cacheable).items()
                      if ref not in details}
            details.update(cached)
        needed = {str(o["OrderRef"]) for o in orders if want(o)} - set(details)
        print(f"EPOS: {len(orders)} POs ordered {order_from}..{order_to}; {len(needed)} detail page(s) to open"
              + (f" ({len(cached)} earlier PO(s) unchanged, read from the cache)" if cached else ""), flush=True)
        by_ref = {str(o["OrderRef"]): o for o in orders}
        for _ in range(3):
            if not needed:
                break
            rows = page.locator(sel).count()
            for i in range(rows):
                if not needed:
                    break
                if page.locator(sel).count() != rows:
                    open_list(page)
                el = page.locator(sel).nth(i)
                text = el.evaluate("e => (e.closest('tr') || e.parentElement).innerText") or ""
                m = re.match(r"\s*(\d+)", text)
                if not m or m.group(1) not in needed:
                    continue
                ref = m.group(1)
                try:
                    el.click()
                    page.wait_for_url(re.compile(r"/PurchaseOrder/Details"), timeout=60000)
                    page.wait_for_load_state("networkidle")
                    d = extract_po_model(page.content())
                except Exception as exc:  # noqa: BLE001 - retried on the next pass
                    print(f"PO {ref}: detail capture failed ({str(exc)[:160]})", flush=True)
                    open_list(page)
                    continue
                if str(d.get("OrderRef")) != ref:
                    print(f"PO {ref}: Details page shows {d.get('OrderRef')}; retrying", flush=True)
                    open_list(page)
                    continue
                d["_captured_at"] = now_iso()
                with open(details_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(d) + "\n")
                details[ref] = d
                needed.discard(ref)
                if cache_path is not None and ref in by_ref:
                    with open(cache_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"ref": ref, "fingerprint": po_fingerprint(by_ref[ref]), "detail": d})
                                 + "\n")
                print(f"PO {ref}: {len(d.get('Products') or [])} line(s)", flush=True)
                open_list(page)
        browser.close()
    if cache_path is not None and Path(cache_path).exists():
        prune_po_cache(cache_path, by_ref)
    if needed:
        print(f"EPOS: detail not captured for {sorted(needed)}", flush=True)
    return orders, details


# ---------------------------------------------------------------- EPOS -> PO records
def build_po(order: dict, detail: dict | None) -> dict:
    recv_at = parse_epos_ts(order.get("DateReceived")) or parse_epos_ts((detail or {}).get("DateCompleted"))
    supplier, payment, note = parse_po_note((detail or {}).get("Note") or order.get("Note"))
    payment = payment or payment_mode((detail or {}).get("PaymentMode") or order.get("PaymentMode"))
    paid, pay_account = payment_detail(note)
    supplier_id = clean((detail or {}).get("SupplierId") or order.get("SupplierId"))
    lines = []
    for n, p in enumerate((detail or {}).get("Products") or [], start=1):
        lines.append({
            "n": n, "epos_line_id": clean(p.get("Id")), "product_id": canonical_product_id(p.get("ProductId")),
            "product": clean(p.get("ProductName")), "qty_ordered": D(p.get("Quantity"), Decimal(0)),
            "qty_received": D(p.get("QuantityReceived"), Decimal(0)),
            "unit_ex": D(p.get("ValueExcTax"), Decimal(0)), "unit_inc": D(p.get("ValueIncTax"), Decimal(0)),
            "tax_pct": D(p.get("TaxRatePercentage"), Decimal(0)),
            "unit_fields": {k: p.get(k) for k in ("VolumeOfSale", "CostPriceMeasurementUnitVolume", "Factor")
                            if p.get(k) not in (None, "", 0)},
        })
    return {
        "ref": str(order["OrderRef"]), "status": clean(order.get("StatusName") or (detail or {}).get("StatusName")),
        "received_at": recv_at.isoformat(timespec="seconds") if recv_at else "",
        "received_date": business_date(recv_at).isoformat() if recv_at else "",
        "supplier": supplier, "supplier_id": "" if supplier_id in ("0", "None") else supplier_id,
        "payment": payment, "payment_paid": paid, "payment_account": pay_account,
        "note": note, "grn": ";".join(order.get("GoodsReceiptNumbers") or []),
        "total_ex": D(order.get("TotalValueReceivedExTax"), Decimal(0)),
        "total_inc": D(order.get("TotalValueReceived"), Decimal(0)),
        "has_detail": detail is not None, "lines": lines,
    }


def po_signature(po: dict) -> dict:
    sig = defaultdict(Decimal)
    for line in po["lines"]:
        if line["qty_received"]:
            sig[line["product_id"]] += line["qty_received"]
    return dict(sig)


def duplicate_of(po: dict, others: list[dict], *, days: int, min_value: Decimal) -> tuple[list, list]:
    """(holds, warnings): earlier POs with the same product set / quantities and a similar amount.

    Rule from the September GRNI review (3828/3829/3855/3861): product-set Jaccard >= 0.75,
    >= 50% of lines with the same qty, received total within 10%, within ``days`` days. The
    supplier is reported, not required (3855 and 3861 name different suppliers). Below
    ``min_value`` (routine bread / water) it is a warning only."""
    a = po_signature(po)
    if not a or not po["received_date"]:
        return [], []
    holds, warns = [], []
    for e in others:
        if e["ref"] == po["ref"] or not e["received_at"] or e["received_at"] >= po["received_at"]:
            continue
        gap = (date.fromisoformat(po["received_date"]) - date.fromisoformat(e["received_date"])).days
        if gap > days:
            continue
        b = po_signature(e)
        if not b:
            continue
        both = set(a) & set(b)
        jaccard = len(both) / len(set(a) | set(b))
        same_qty = sum(1 for k in both if a[k] == b[k]) / max(len(a), len(b))
        big = max(po["total_inc"], e["total_inc"])
        close = big > 0 and abs(po["total_inc"] - e["total_inc"]) <= Decimal("0.10") * big
        if jaccard >= 0.75 and same_qty >= 0.5 and close:
            text = (f"possible duplicate receipt of EPOS PO {e['ref']} ({e['received_date']}, "
                    f"{e['supplier'] or 'no supplier'}, {money(e['total_inc'])}): same products/qty")
            (holds if po["total_inc"] >= min_value else warns).append(text)
    return holds, warns


# ---------------------------------------------------------------- vendor map
def load_vendor_map(rows: list[dict]) -> tuple[dict, dict, list]:
    """Approved rows only -> ({supplier id: row}, {normalized name: row}, conflicts)."""
    by_id, by_name, conflicts = {}, {}, []
    for r in rows:
        if not clean(r.get("Approved By")) or not clean(r.get("QBO Vendor Id")):
            continue
        for idx, key in ((by_id, clean(r.get("EPOS Supplier Id"))), (by_name, vendor_key(r.get("EPOS Supplier Name")))):
            if not key or key == "0":
                continue
            prior = idx.get(key)
            if prior and clean(prior["QBO Vendor Id"]) != clean(r["QBO Vendor Id"]):
                conflicts.append(key)
            idx[key] = r
    return by_id, by_name, conflicts


def resolve_vendor(po: dict, vmap, vendors_by_id: dict) -> tuple[dict | None, str]:
    by_id, by_name, conflicts = vmap
    row = by_id.get(po["supplier_id"]) if po["supplier_id"] else None
    key = vendor_key(po["supplier"])
    if row is None:
        if not key:
            return None, "no supplier on the EPOS PO note (and no EPOS supplier id) - fix the PO note or map it"
        if key in conflicts:
            return None, f"vendors.csv maps supplier '{po['supplier']}' to more than one QBO vendor"
        row = by_name.get(key)
    if row is None:
        return None, f"supplier '{po['supplier']}' not approved in vendors.csv - run vendors-suggest"
    vid = clean(row["QBO Vendor Id"])
    live = vendors_by_id.get(vid)
    if live is None:
        return None, f"vendors.csv QBO Vendor Id {vid} not found in QBO"
    if live.get("Active") is False:
        return None, f"QBO vendor {vid} {live.get('DisplayName')} is inactive"
    if clean(row.get("QBO Vendor Name")) and clean(row["QBO Vendor Name"]).casefold() != clean(live.get("DisplayName")).casefold():
        return None, (f"QBO vendor {vid} is now '{live.get('DisplayName')}', vendors.csv says "
                      f"'{row['QBO Vendor Name']}' - re-approve the row")
    return live, ""


# ---------------------------------------------------------------- items
def item_problems(item: dict | None, rule) -> list[str]:
    """Why a live QBO item may not receive an October purchase line ([] when fine)."""
    if item is None:
        return [f"QBO item {rule.target_qbo_item_id} not found"]
    out = []
    name, sku = clean(item.get("Name")), clean(item.get("Sku"))
    if str(item.get("Id")) == CATCH_ALL_ITEM_ID:
        out.append("target is the pre-October catch-all 15030")
    if name.startswith(LEGACY_PREFIX) or clean(item.get("FullyQualifiedName")).startswith(LEGACY_PREFIX):
        out.append(f"target {item.get('Id')} is a LEGACY item")
    if item.get("Active") is False:
        out.append(f"QBO item {item.get('Id')} is inactive")
    if item.get("Type") != rule.target_qbo_type:
        out.append(f"QBO item type {item.get('Type')} != mapping {rule.target_qbo_type}")
    if sku != rule.target_qbo_sku:
        out.append(f"QBO item Sku {sku!r} != mapping {rule.target_qbo_sku!r}")
    reason = october_target_error(item.get("Type"), sku, item.get("Id"), name)
    if reason:
        out.append(reason)
    for ref in ("AssetAccountRef", "ExpenseAccountRef", "IncomeAccountRef"):
        if clean((item.get(ref) or {}).get("name")).startswith("120"):
            out.append(f"{ref} is a 120xxx account")
    if item.get("Type") == "Inventory" and clean((item.get("AssetAccountRef") or {}).get("value")) != ASSET_ID:
        out.append(f"Inventory asset account {(item.get('AssetAccountRef') or {}).get('value')} != 77")
    if item.get("Type") == "NonInventory" and clean((item.get("ExpenseAccountRef") or {}).get("value")) not in EXPENSE_IDS:
        out.append(f"NonInventory expense account {(item.get('ExpenseAccountRef') or {}).get('value')} not 200xxx")
    return out


def fetch_items(client: QBOClient, ids) -> dict:
    ids = sorted({i for i in ids if i})
    out = {}
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        sql = "select * from Item where Id in (" + ",".join(f"'{qbo_escape(i)}'" for i in chunk) + ")"
        for it in as_list(client.query(sql + " maxresults 1000").get("Item")):
            out[str(it["Id"])] = it
    return out


# ---------------------------------------------------------------- QBO context (GET only)
def fetch_context(client: QBOClient, pos: list[dict], registry, *, bills_from: str, bills_to: str) -> dict:
    item_ids = set()
    for po in pos:
        for line in po["lines"]:
            try:
                rule = registry.resolve(product_name=line["product"], product_id=line["product_id"],
                                        transaction_date=date.fromisoformat(po["received_date"] or INV_START))
            except ProductResolutionError:
                continue
            item_ids.add(rule.target_qbo_item_id)
    docs = [doc_number(po["ref"]) for po in pos]
    by_doc = []
    for start in range(0, len(docs), 50):
        chunk = docs[start:start + 50]
        sql = "select * from Bill where DocNumber in (" + ",".join(f"'{qbo_escape(d)}'" for d in chunk) + ")"
        by_doc += as_list(client.query(sql + " maxresults 1000").get("Bill"))
    window = client.query_all(f"select * from Bill where TxnDate >= '{bills_from}' and TxnDate <= '{bills_to}'", "Bill")
    bills = {b["Id"]: b for b in window + by_doc}
    prefs = client.get_json("/preferences").get("Preferences", {})
    return {
        "items": fetch_items(client, item_ids),
        "vendors": {v["Id"]: v for v in client.query_all("select * from Vendor where Active in (true,false)", "Vendor")},
        "bills": list(bills.values()),
        "terms": {t["Id"]: t for t in client.query_all("select * from Term", "Term")},
        "book_close": clean((prefs.get("AccountingInfoPrefs") or {}).get("BookCloseDate")),
    }


# ---------------------------------------------------------------- bill checks
def manual_duplicates(bills: list[dict], *, vendor_id: str, txn_date: str, totals: list[Decimal], item_ids: set,
                      doc: str) -> list[str]:
    """Manual bills (not created by bills_sync) that look like this receipt."""
    out = []
    d0 = date.fromisoformat(txn_date)
    for b in bills:
        bdoc = clean(b.get("DocNumber"))
        if bdoc.startswith(DOC_PREFIX) or bdoc == doc:
            continue
        gap = abs((date.fromisoformat(b["TxnDate"]) - d0).days)
        amt = D(b.get("TotalAmt"), Decimal(0))
        same_vendor = clean((b.get("VendorRef") or {}).get("value")) == vendor_id and vendor_id
        label = f"Bill {b['Id']} {b['TxnDate']} {(b.get('VendorRef') or {}).get('name')} {money(amt)}"
        if same_vendor and gap <= 5:
            if any(t > 0 and abs(amt - t) <= Decimal("0.01") * t for t in totals):
                out.append(f"possible manual duplicate: {label} (same vendor, amount within 1%)")
                continue
            lines = {clean(((ln.get("ItemBasedExpenseLineDetail") or {}).get("ItemRef") or {}).get("value"))
                     for ln in b.get("Line") or []}
            if item_ids and len(item_ids & lines) >= 0.5 * len(item_ids):
                out.append(f"possible manual duplicate: {label} (same vendor, same items)")
                continue
        if gap <= 1 and any(t > 0 and abs(amt - t) <= max(TOL, Decimal("0.0005") * t) for t in totals):
            out.append(f"possible manual duplicate: {label} (same amount, other vendor)")
    return out


def due_date(txn_date: str, vendor: dict, terms: dict) -> tuple[str, dict | None]:
    term_id = clean((vendor.get("TermRef") or {}).get("value"))
    term = terms.get(term_id) if term_id else None
    days = D((term or {}).get("DueDays"))
    if term and days is not None:
        return (date.fromisoformat(txn_date) + timedelta(days=int(days))).isoformat(), {"value": term_id}
    return txn_date, None


def line_tax(line: dict, tax_mode: str) -> tuple[Decimal, str]:
    if tax_mode == "gross":
        return r2(line["qty_received"] * line["unit_inc"]), TAX_NO_VAT
    code = TAX_STANDARD if line["tax_pct"] > 0 else TAX_NO_VAT
    return r2(line["qty_received"] * line["unit_ex"]), code


def expected_total(lines: list[dict]) -> Decimal:
    net = sum((ln["amount"] for ln in lines), Decimal(0))
    vat = sum((ln["amount"] for ln in lines if ln["tax_code"] == TAX_STANDARD), Decimal(0)) * VAT_RATE
    return r2(net + r2(vat))


# ---------------------------------------------------------------- plan (pure)
def plan_bills(pos: list[dict], *, registry, ctx: dict, vmap, window: tuple[str, str], tax_mode: str = "gross",
               lookback: list[dict] = (), dup_days: int = 14, dup_min_value: Decimal = Decimal("50000"),
               exclusions=None) -> list[dict]:
    """One entry per PO received in ``window``. No HTTP.

    ``exclusions`` (``review_exclusions.Exclusions``): an excluded PO (kind ``bill``) is ``EXCLUDED``
    (resolved outside the tool, never posted); an excluded supplier (kind ``vendor``) HOLDs."""
    items, vendors, bills = ctx["items"], ctx["vendors"], ctx["bills"]
    bills_by_doc = defaultdict(list)
    for b in bills:
        if clean(b.get("DocNumber")):
            bills_by_doc[clean(b["DocNumber"])].append(b)
    in_window = sorted((p for p in pos if window[0] <= p["received_date"] <= window[1]),
                       key=lambda p: (p["received_at"], p["ref"]))
    history = list(lookback) + in_window
    entries = []
    for po in in_window:
        doc = doc_number(po["ref"])
        e = {"po": po, "doc": doc, "status": "", "reasons": [], "warnings": [], "lines": [], "vendor": None,
             "payload": None, "payload_sha256": "", "bill_total": Decimal(0)}
        entries.append(e)
        reasons, warns = e["reasons"], e["warnings"]
        if po["received_date"] < INV_START:
            e["status"] = "EXCLUDED"
            reasons.append(SEPTEMBER_NOTE)
            continue
        po_excluded = exclusions.describe("bill", po["ref"]) if exclusions is not None else ""
        if po_excluded:
            e["status"] = "EXCLUDED"
            reasons.append(f"{PO_EXCLUDED_NOTE}: {po_excluded}")
            continue
        supplier_excluded = (exclusions.describe("vendor", po["supplier"])
                             if exclusions is not None and clean(po["supplier"]) else "")
        if supplier_excluded:
            reasons.append(f"supplier excluded: {supplier_excluded}")
        if po["status"].casefold() not in RECEIVED_STATUSES:
            reasons.append(f"EPOS status '{po['status']}' is not received")
        if not po["has_detail"]:
            reasons.append("EPOS PO detail not captured - re-run plan")
        received = [ln for ln in po["lines"] if ln["qty_received"] != 0]
        if po["has_detail"] and not received:
            e["status"] = "SKIP"
            reasons.append("no quantity received")
            continue
        vendor, why = resolve_vendor(po, vmap, vendors)
        e["vendor"] = vendor
        if why:
            reasons.append(why)
        txn = po["received_date"]
        unmapped = []
        for ln in received:
            row = {"src": ln, "flags": [], "rule": None, "item": None}
            e["lines"].append(row)
            if ln["qty_received"] < 0:
                row["flags"].append("negative quantity received")
            if ln["unit_fields"]:
                row["flags"].append(f"PO line carries unit fields {ln['unit_fields']} - unit rule unproven")
            if ln["unit_ex"] <= 0 or ln["unit_inc"] <= 0:
                row["flags"].append("zero cost on the EPOS line (never invent cost)")
            try:
                rule = registry.resolve(product_name=ln["product"], product_id=ln["product_id"],
                                        transaction_date=date.fromisoformat(txn))
            except ProductResolutionError as exc:
                unmapped.append(f"{ln['product_id']} {ln['product']} ({exc.code})")
                continue
            row["rule"] = rule
            reason = october_target_error(rule.target_qbo_type, rule.target_qbo_sku, rule.target_qbo_item_id,
                                          rule.target_qbo_name)
            if reason:
                row["flags"].append(f"mapping target refused: {reason}")
            if not rule.target_qbo_item_id:
                row["flags"].append("mapping row has no Target QBO Item Id")
                continue
            own = {AKP_SKU_PREFIX + ln["product_id"], AKP_NS_SKU_PREFIX + ln["product_id"]}
            if rule.target_qbo_sku not in own:
                row["flags"].append(f"PO is on EPOS product {ln['product_id']}, a child of {rule.target_qbo_sku}; "
                                    "EPOS stock effect of receiving a non-master product is unproven - "
                                    "re-enter the receipt on the master")
            item = items.get(rule.target_qbo_item_id)
            row["item"] = item
            row["flags"] += item_problems(item, rule)
            mult = rule.purchase_multiplier
            row["qbo_qty"] = ln["qty_received"] * mult
            row["amount"], row["tax_code"] = line_tax(ln, tax_mode)
            row["unit_cost"] = q(row["amount"] / row["qbo_qty"], "0.00001") if row["qbo_qty"] else Decimal(0)
            unit_ex = ln["unit_ex"] / mult
            pc = D((item or {}).get("PurchaseCost"), Decimal(0))
            row["cost_ratio"] = q(unit_ex / pc, "0.001") if pc > 0 else None
            if row["cost_ratio"] is not None and not (Decimal("0.5") <= row["cost_ratio"] <= Decimal("2")):
                row["flags"].append(f"cost per {rule.canonical_unit or 'unit'} {q(unit_ex, '0.01')} is "
                                    f"{row['cost_ratio']}x the QBO PurchaseCost {pc} - multiplier x{mult} suspect")
            if ln["qty_received"] != ln["qty_ordered"]:
                warns.append(f"line {ln['n']} partial receipt: {qty_text(ln['qty_received'])} of "
                             f"{qty_text(ln['qty_ordered'])} received")
        if unmapped:
            reasons.append(f"{len(unmapped)} product(s) not in the approved mapping - run catalogue_sync: "
                           + "; ".join(unmapped[:5]))
        for row in e["lines"]:
            for flag in row["flags"]:
                reasons.append(f"line {row['src']['n']} {row['src']['product_id']} {row['src']['product']}: {flag}")
        line_ex = sum((r2(ln["qty_received"] * ln["unit_ex"]) for ln in received), Decimal(0))
        line_inc = sum((r2(ln["qty_received"] * ln["unit_inc"]) for ln in received), Decimal(0))
        if abs(line_ex - po["total_ex"]) > TOL or abs(line_inc - po["total_inc"]) > TOL:
            reasons.append(f"EPOS line sum ex {money(line_ex)} / inc {money(line_inc)} != PO received total "
                           f"ex {money(po['total_ex'])} / inc {money(po['total_inc'])}")
        priced = [r for r in e["lines"] if "amount" in r]
        if len(priced) == len(received):
            e["bill_total"] = expected_total(priced)
            if abs(e["bill_total"] - po["total_inc"]) > TOL:
                reasons.append(f"bill total {money(e['bill_total'])} != EPOS received inc-tax {money(po['total_inc'])}")
        if ctx.get("book_close") and txn <= ctx["book_close"]:
            reasons.append(f"TxnDate {txn} is inside the QBO closed period (BookCloseDate {ctx['book_close']})")
        existing = bills_by_doc.get(doc, [])
        if existing:
            b = existing[0]
            if len(existing) == 1 and abs(D(b.get("TotalAmt"), Decimal(0)) - e["bill_total"]) <= TOL \
                    and e["bill_total"] > 0:
                e["status"] = "SKIP"
                e["reasons"] = [f"already posted as Bill {b['Id']} ({b['TxnDate']}, {money(D(b['TotalAmt']))})"]
                e["existing_bill_id"] = b["Id"]
                continue
            reasons.append(f"Bill(s) {', '.join(x['Id'] for x in existing)} already carry DocNumber {doc} "
                           f"but differ (total {money(D(b.get('TotalAmt'), Decimal(0)))}) - later receipt or edit?")
        if vendor:
            reasons += manual_duplicates(bills, vendor_id=vendor["Id"], txn_date=txn,
                                         totals=[po["total_inc"], po["total_ex"]],
                                         item_ids={r["rule"].target_qbo_item_id for r in e["lines"] if r["rule"]},
                                         doc=doc)
        holds, dwarns = duplicate_of(po, history, days=dup_days, min_value=dup_min_value)
        if holds and exclusions is not None and exclusions.describe("repeat_ok", po["ref"]):
            e["routine_repeat"] = holds  # confirmed a real order in the portal: noted, not held
            holds = []
        reasons += holds
        routine = (exclusions.describe("routine_repeat", po["supplier"])
                   if dwarns and exclusions is not None and clean(po["supplier"]) else "")
        if routine:
            # A routine supplier (owner's "routine repeat orders" list): the repeat is noted, not blocking.
            e["routine_repeat"] = dwarns
        else:
            warns += dwarns
        if reasons:
            e["status"] = "HOLD"
            continue
        e["status"] = "READY"
        due, term_ref = due_date(txn, vendor, ctx.get("terms", {}))
        hint = (po["payment"] or "not stated") + (f" {po['payment_paid']}" if po.get("payment_paid") else "") + (
            f" from {po['payment_account']}" if po.get("payment_account") else "")
        note = (f"Payment hint: {hint} (EPOS PO note; bill left UNPAID - pay in QBO) | "
                f"EPOS PO {po['ref']} | GRN {po['grn'] or '-'} | received {po['received_at']} | supplier "
                f"{po['supplier'] or '-'} | created by {TOOL} (tax mode {tax_mode})")
        payload = {
            "VendorRef": {"value": vendor["Id"], "name": vendor.get("DisplayName")},
            "APAccountRef": {"value": AP_ACCOUNT_ID}, "TxnDate": txn, "DueDate": due, "DocNumber": doc,
            "DepartmentRef": {"value": DEPARTMENT_ID}, "CurrencyRef": {"value": "NGN"},
            "GlobalTaxCalculation": "TaxExcluded", "PrivateNote": note[:4000],
            "Line": [{
                "DetailType": "ItemBasedExpenseLineDetail", "Amount": float(r["amount"]),
                "Description": (f"EPOS PO {po['ref']}: {r['src']['product']} (EPOS {r['src']['product_id']}) "
                                f"{qty_text(r['src']['qty_received'])} x{qty_text(r['rule'].purchase_multiplier)} "
                                f"= {qty_text(r['qbo_qty'])} {r['rule'].canonical_unit or 'units'}")[:4000],
                "ItemBasedExpenseLineDetail": {
                    "ItemRef": {"value": r["rule"].target_qbo_item_id, "name": r["item"].get("Name")},
                    "Qty": float(r["qbo_qty"]), "UnitPrice": float(r["unit_cost"]),
                    "TaxCodeRef": {"value": r["tax_code"]}, "BillableStatus": "NotBillable"}}
                for r in e["lines"]],
        }
        if term_ref:
            payload["SalesTermRef"] = term_ref
        e["payload"] = payload
        e["payload_sha256"] = sha256_text(canonical_json(payload))
        e["requestid"] = requestid_for(doc, e["payload_sha256"])
    return entries


# ---------------------------------------------------------------- evidence
def review_rows(entries: list[dict]) -> list[dict]:
    return [{
        "PO": e["po"]["ref"], "Received At": e["po"]["received_at"], "Received Date": e["po"]["received_date"],
        "GRN": e["po"]["grn"], "EPOS Supplier": e["po"]["supplier"], "Payment Mode": e["po"]["payment"],
        "QBO Vendor Id": (e["vendor"] or {}).get("Id", ""), "QBO Vendor Name": (e["vendor"] or {}).get("DisplayName", ""),
        "Lines": len(e["lines"]), "EPOS Total Ex": money(e["po"]["total_ex"]), "EPOS Total Inc": money(e["po"]["total_inc"]),
        "Bill Total": money(e["bill_total"]) if e["bill_total"] else "", "DocNumber": e["doc"], "Status": e["status"],
        "Reasons": " | ".join(e["reasons"]), "Warnings": " | ".join(e["warnings"]),
        "Payload SHA": e["payload_sha256"], "Approve": "",
    } for e in entries]


def line_rows(entries: list[dict]) -> list[dict]:
    rows = []
    for e in entries:
        for r in e["lines"]:
            ln, rule, item = r["src"], r["rule"], r["item"] or {}
            onhand = D(item.get("QtyOnHand"))
            after = onhand + r["qbo_qty"] if onhand is not None and "qbo_qty" in r and item.get("Type") == "Inventory" else None
            rows.append({
                "PO": e["po"]["ref"], "Line": ln["n"], "EPOS Product ID": ln["product_id"], "EPOS Product": ln["product"],
                "Qty Ordered": qty_text(ln["qty_ordered"]), "Qty Received": qty_text(ln["qty_received"]),
                "Multiplier": qty_text(rule.purchase_multiplier) if rule else "",
                "QBO Item Id": rule.target_qbo_item_id if rule else "", "QBO Item Name": item.get("Name", ""),
                "QBO Sku": item.get("Sku", ""), "QBO Type": item.get("Type", ""),
                "QBO Qty": qty_text(r["qbo_qty"]) if "qbo_qty" in r else "",
                "Unit Cost": str(r.get("unit_cost", "")), "Amount": money(r["amount"]) if "amount" in r else "",
                "TaxCode": r.get("tax_code", ""), "EPOS Unit Ex": str(ln["unit_ex"]), "EPOS Unit Inc": str(ln["unit_inc"]),
                "Tax %": str(ln["tax_pct"]), "QBO PurchaseCost": item.get("PurchaseCost", ""),
                "Cost Ratio": str(r.get("cost_ratio") or ""), "QBO QtyOnHand": "" if onhand is None else qty_text(onhand),
                "QtyOnHand After": "" if after is None else qty_text(after), "Line Flags": " | ".join(r["flags"]),
            })
    return rows


def payload_lines(entries: list[dict]) -> list[dict]:
    return [{"po": e["po"]["ref"], "doc_number": e["doc"], "received_date": e["po"]["received_date"],
             "vendor_id": e["vendor"]["Id"], "bill_total": money(e["bill_total"]),
             "epos_total_inc": money(e["po"]["total_inc"]), "epos_total_ex": money(e["po"]["total_ex"]),
             "item_ids": [ln["ItemBasedExpenseLineDetail"]["ItemRef"]["value"] for ln in e["payload"]["Line"]],
             "rules": [{"product_id": r["src"]["product_id"], "item_id": r["rule"].target_qbo_item_id,
                        "multiplier": str(r["rule"].purchase_multiplier)} for r in e["lines"]],
             "warnings": e["warnings"], "payload": e["payload"], "payload_sha256": e["payload_sha256"],
             "requestid": e["requestid"]}
            for e in entries if e["status"] == "READY"]


REASON_CATEGORIES = (
    ("supplier excluded", "supplier excluded (review_exclusions)"),
    ("not approved in vendors.csv", "vendor not in vendors.csv"),
    ("no supplier on the EPOS PO note", "no supplier on PO note"),
    ("QBO vendor", "QBO vendor problem"),
    ("not in the approved mapping", "product not in mapping (run catalogue_sync)"),
    ("a child of", "PO on a non-master EPOS product"),
    ("unit fields", "PO line carries unit fields"),
    ("x the QBO PurchaseCost", "unit cost vs QBO PurchaseCost (multiplier?)"),
    ("zero cost", "zero cost line"),
    ("LEGACY", "LEGACY / catch-all target"),
    ("catch-all", "LEGACY / catch-all target"),
    ("possible duplicate receipt", "possible duplicate EPOS PO"),
    ("possible manual duplicate", "possible manual QBO bill"),
    ("already carry DocNumber", "DocNumber exists with a different total"),
    ("!= PO received total", "EPOS line sum != PO total"),
    ("bill total", "bill total != EPOS total"),
    ("closed period", "closed period"),
    ("detail not captured", "PO detail not captured"),
    ("is not received", "PO not received"),
)


def reason_category(reason: str) -> str:
    for needle, label in REASON_CATEGORIES:
        if needle in reason:
            return label
    return reason[:60]


def write_xlsx(path: Path, review: list[dict], lines: list[dict]) -> None:
    wb = Workbook()
    for ws, cols, rows in ((wb.active, REVIEW_COLS, review), (wb.create_sheet("Lines"), LINE_COLS, lines)):
        ws.title = "Review" if cols is REVIEW_COLS else "Lines"
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c, "") for c in cols])
        ws.freeze_panes = "A2"
    wb.save(path)


def write_plan(out: Path, entries: list[dict], meta: dict) -> dict:
    review, lines = review_rows(entries), line_rows(entries)
    write_csv(out / "review.csv", review, REVIEW_COLS)
    write_csv(out / "review_lines.csv", lines, LINE_COLS)
    write_xlsx(out / "review.xlsx", review, lines)
    text = "".join(canonical_json(p) + "\n" for p in payload_lines(entries))
    (out / "payloads.jsonl").write_text(text, encoding="utf-8")
    counts = Counter(e["status"] for e in entries)
    hold_reasons = Counter()
    for e in entries:
        if e["status"] == "HOLD":
            for cat in {reason_category(r) for r in e["reasons"]}:
                hold_reasons[cat] += 1
    summary = {
        **meta, "pos_in_window": len(entries), "counts": dict(counts),
        "ready_total": money(sum((e["bill_total"] for e in entries if e["status"] == "READY"), Decimal(0))),
        "hold_total_inc": money(sum((e["po"]["total_inc"] for e in entries if e["status"] == "HOLD"), Decimal(0))),
        "window_total_inc": money(sum((e["po"]["total_inc"] for e in entries), Decimal(0))),
        "window_total_ex": money(sum((e["po"]["total_ex"] for e in entries), Decimal(0))),
        "hold_reason_counts": dict(hold_reasons.most_common()),
        "holds": [{"po": e["po"]["ref"], "supplier": e["po"]["supplier"], "total_inc": money(e["po"]["total_inc"]),
                   "reasons": e["reasons"]} for e in entries if e["status"] == "HOLD"],
        "routine_repeats": [{"po": e["po"]["ref"], "supplier": e["po"]["supplier"], "note": e["routine_repeat"][0]}
                            for e in entries if e.get("routine_repeat")],
        "payloads_sha256": sha256_file(out / "payloads.jsonl"), "payload_count": len(payload_lines(entries)),
    }
    summary["post_command"] = (f".venv/bin/python -m code_scripts.akponora_ops.{TOOL} post --review "
                               f"{out / 'review.csv'} --approval-ref '<chat yes>' --expect-sha "
                               f"{summary['payloads_sha256']}")
    dump_json(out / "plan.json", {**summary, "entries": [{
        "po": {k: v for k, v in e["po"].items() if k != "lines"}, "status": e["status"], "doc": e["doc"],
        "reasons": e["reasons"], "warnings": e["warnings"], "bill_total": money(e["bill_total"]),
        "vendor_id": (e["vendor"] or {}).get("Id", ""), "payload_sha256": e["payload_sha256"],
        "existing_bill_id": e.get("existing_bill_id", "")} for e in entries]})
    dump_json(out / "summary.json", summary)
    return summary


def slack_text(summary: dict, out: Path, posted: dict | None = None) -> str:
    c = summary["counts"]
    head = (f"Akponora bills_sync plan {summary['window'][0]}..{summary['window'][1]}: "
            f"{summary['pos_in_window']} EPOS PO(s) received; READY {c.get('READY', 0)} "
            f"(N{summary['ready_total']}), HOLD {c.get('HOLD', 0)} (N{summary['hold_total_inc']}), "
            f"SKIP {c.get('SKIP', 0)}, EXCLUDED {c.get('EXCLUDED', 0)}.")
    lines = [head]
    for h in summary["holds"][:10]:
        lines.append(f"- HOLD PO {h['po']} {h['supplier'] or '(no supplier)'} N{h['total_inc']}: {h['reasons'][0][:160]}")
    for v in summary.get("vendor_actions", [])[:10]:
        lines.append(f"- vendor {v['state']}: {v['detail'][:200]}")
    if posted:
        lines.append(f"Posted {posted.get('POSTED', 0)}, adopted {posted.get('ADOPTED', 0)}, "
                     f"held live {posted.get('HELD_LIVE', 0)}, capped {posted.get('CAPPED', 0)} (bills left unpaid).")
    lines.append(f"Review: {out}/review.csv (set Approve=yes, then post with --expect-sha {summary['payloads_sha256'][:12]}...)")
    return "\n".join(lines)


# ---------------------------------------------------------------- vendors (automatic creation)
def unmapped_suppliers(pos: list[dict], vmap, window: tuple[str, str]) -> list[dict]:
    """Suppliers of October POs received in ``window`` that vendors.csv does not resolve
    (id or name), grouped by normalized name."""
    by_id, by_name, conflicts = vmap
    groups: dict[str, dict] = {}
    for po in pos:
        if not (window[0] <= po["received_date"] <= window[1]) or po["received_date"] < INV_START:
            continue
        if not any(ln["qty_received"] for ln in po["lines"]):
            continue
        if po["supplier_id"] and po["supplier_id"] in by_id:
            continue
        key = vendor_key(po["supplier"])
        if not key or key in by_name or key in conflicts:
            continue
        g = groups.setdefault(key, {"key": key, "name": po["supplier"], "supplier_id": po["supplier_id"],
                                    "po_refs": set()})
        g["po_refs"].add(po["ref"])
        g["supplier_id"] = g["supplier_id"] or po["supplier_id"]
    return [groups[k] for k in sorted(groups)]


def vendor_stage(pos, vmap, ctx, *, window, vendors_path: Path, create: bool, write_client=None,
                 history: Path | None = None, exclusions=None) -> list[dict]:
    """Score unmapped suppliers; in ``create`` mode (scheduled) create genuinely new vendors when the
    auto gates allow (never an excluded supplier). Updates ``ctx['vendors']`` with created vendors.
    Returns the actions."""
    suppliers = unmapped_suppliers(pos, vmap, window)
    if exclusions is not None:
        po_skip = exclusions.keys("bill")
        suppliers = [s for s in suppliers if not set(s["po_refs"]) <= po_skip]
    if not suppliers:
        return []
    hist = history_suppliers(history) if history and Path(history).exists() else {}
    actions = vendor_ops.plan_actions(suppliers, ctx["vendors"], history=hist, bill_counts=ctx.get("bill_counts"),
                                      exclusions=exclusions)
    if not create:
        for act in actions:
            if act["state"] == vendor_ops.LINK:
                act["detail"] += " - plan only; scheduled mode records the link"
            if act["state"] == vendor_ops.CREATE:
                act["detail"] += f" - plan only; scheduled mode creates it when {vendor_ops.AUTO_ENV}=1"
        return actions
    settings = vendor_ops.auto_settings()
    wants = [a for a in actions if a["state"] == vendor_ops.CREATE]
    client = None
    if wants and settings is not None and settings["max"] > 0:
        client = write_client or QBOClient.for_company_a(allow_writes=True)
    vendor_ops.apply_actions(actions, client=client, vendors_path=vendors_path, settings=settings)
    for act in actions:
        if act["state"] == vendor_ops.CREATED:
            ctx["vendors"][act["vendor_id"]] = act.pop("vendor")
    return actions


def vendor_reason(act: dict) -> str:
    return f"vendor: {act['state']} - {act['detail']}"


# ---------------------------------------------------------------- plan (CLI)
def load_registry(path: Path | None) -> ProductConversionRegistry:
    return ProductConversionRegistry.from_csv(path or mapping_file(), allow_name_fallback=False)


def load_pos(po_list: Path, po_details: Path) -> tuple[list, dict]:
    raw = json.loads(Path(po_list).read_text())
    body = raw.get("body", raw)
    orders = body["orders"] if "orders" in body else body.get("body", {}).get("orders", [])
    details = {}
    for line in Path(po_details).read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            details[str(d["OrderRef"])] = d
    return orders, details


def collect_pos(a, ev: Path, window: tuple[str, str]) -> tuple[list, list]:
    """(window POs, lookback POs) from offline files or a live view-only EPOS capture."""
    lookback_from = (date.fromisoformat(window[0]) - timedelta(days=a.dup_days)).isoformat()

    def recv_date(o):
        ts = parse_epos_ts(o.get("DateReceived"))
        return business_date(ts).isoformat() if ts else ""

    def want(o):
        d = recv_date(o)
        if window[0] <= d <= window[1]:
            return True
        return lookback_from <= d < window[0] and D(o.get("TotalValueReceived"), Decimal(0)) >= Decimal(a.dup_min_value) * Decimal("0.9")

    if a.po_list and a.po_details:
        orders, details = load_pos(a.po_list, a.po_details)
    else:
        order_from = (date.fromisoformat(lookback_from) - timedelta(days=7)).isoformat()
        order_to = (date.fromisoformat(window[1]) + timedelta(days=1)).isoformat()
        # Earlier POs are only read for the duplicate check and never posted: an unchanged one is read
        # from the cache. POs in the window (the ones that get posted) are always opened live.
        orders, details = capture_epos(ev, order_from=order_from, order_to=order_to, want=want,
                                       cacheable=lambda o: recv_date(o) < window[0],
                                       cache_path=state_dir(TOOL) / PO_CACHE)
    pos, lookback = [], []
    for o in orders:
        d = recv_date(o)
        if window[0] <= d <= window[1]:
            pos.append(build_po(o, details.get(str(o["OrderRef"]))))
        elif want(o) and str(o["OrderRef"]) in details:
            lookback.append(build_po(o, details[str(o["OrderRef"])]))
    return pos, lookback


def run_plan(a, client: QBOClient | None = None, write_client: QBOClient | None = None) -> tuple[Path, dict, list]:
    out = run_dir(TOOL, a.out)
    ev = out / "evidence"
    ev.mkdir(exist_ok=True)
    captured_at = today_lagos()
    window = (a.date_from or default_from(), a.date_to or business_date(captured_at).isoformat())
    if window[0] > window[1]:
        raise StopRun(f"empty window {window}")
    vpath = Path(a.vendors) if a.vendors else vendor_file()
    ensure_vendor_file(vpath)
    registry = load_registry(Path(a.mapping) if a.mapping else None)
    pos, lookback = collect_pos(a, ev, window)
    client = client or QBOClient.for_company_a(allow_writes=False)
    bills_from = (date.fromisoformat(window[0]) - timedelta(days=max(a.dup_days, 5) + 5)).isoformat()
    bills_to = (date.fromisoformat(window[1]) + timedelta(days=5)).isoformat()
    ctx = fetch_context(client, pos, registry, bills_from=bills_from, bills_to=bills_to)
    vmap = load_vendor_map(read_csv(vpath))
    ctx["bill_counts"] = Counter((b.get("VendorRef") or {}).get("value") for b in ctx["bills"])
    history = Path(a.history) if getattr(a, "history", None) else (DEFAULT_HISTORY if hasattr(a, "history") else None)
    xpath = Path(getattr(a, "exclusions", None) or review_exclusions.path_near(vpath))
    exclusions = review_exclusions.load(xpath)
    vendor_actions = vendor_stage(pos, vmap, ctx, window=window, vendors_path=vpath,
                                  create=bool(getattr(a, "create_vendors", False)), write_client=write_client,
                                  history=history, exclusions=exclusions)
    if any(act["state"] in (vendor_ops.CREATED, vendor_ops.LINKED) for act in vendor_actions):
        vmap = load_vendor_map(read_csv(vpath))
    entries = plan_bills(pos, registry=registry, ctx=ctx, vmap=vmap, window=window, tax_mode=a.tax_mode,
                         lookback=lookback, dup_days=a.dup_days, dup_min_value=Decimal(a.dup_min_value),
                         exclusions=exclusions)
    by_key = {act["key"]: act for act in vendor_actions
              if act["state"] not in (vendor_ops.CREATED, vendor_ops.LINKED, vendor_ops.EXCLUDED)}
    for e in entries:
        act = by_key.get(vendor_key(e["po"]["supplier"]))
        if act and e["status"] == "HOLD":
            e["reasons"].append(vendor_reason(act))
    dump_json(out / "vendor_actions.json", vendor_actions)
    meta = {"tool": TOOL, "realm": REALM, "mode": "plan", "window": list(window), "tax_mode": a.tax_mode,
            "captured_at_lagos": captured_at.isoformat(timespec="seconds"),
            "closed_through": min(window[1], (business_date(captured_at) - timedelta(days=1)).isoformat()),
            "mapping": {"path": str(Path(a.mapping) if a.mapping else mapping_file()), "sha256": registry.source_sha256},
            "vendors_file": {"path": str(vpath), "sha256": sha256_file(vpath)},
            "exclusions_file": {"path": str(xpath), "sha256": exclusions.sha256(),
                                "active": len(exclusions.rows)},
            "lookback_pos": len(lookback), "qbo_bills_checked": len(ctx["bills"]), "book_close": ctx["book_close"],
            "qbo_requests": client.requests,
            "vendor_actions": [{k: act[k] for k in ("state", "epos_name", "display_name", "vendor_id", "best_score",
                                                    "candidates", "po_refs", "detail")} for act in vendor_actions]}
    summary = write_plan(out, entries, meta)
    return out, summary, entries


def cmd_plan(a) -> int:
    out, summary, _ = run_plan(a)
    print(json.dumps({k: summary[k] for k in ("window", "pos_in_window", "counts", "ready_total", "hold_total_inc",
                                              "hold_reason_counts", "payloads_sha256")}, indent=1))
    if not a.no_slack:
        send_slack(slack_text(summary, out))
    print(f"-> {out}")
    return 0


# ---------------------------------------------------------------- post
def load_results(path: Path) -> dict:
    return {r["PO"]: r for r in read_csv(path)} if path.exists() else {}


def append_result(path: Path, row: dict) -> None:
    rows = read_csv(path) if path.exists() else []
    write_csv(path, rows + [row], RESULT_COLS)


def mapping_drift(p: dict, registry) -> str:
    """Why the current mapping no longer gives this bill's lines ("" when unchanged)."""
    for r in p["rules"]:
        try:
            rule = registry.resolve(product_name="", product_id=r["product_id"],
                                    transaction_date=date.fromisoformat(p["received_date"]))
        except ProductResolutionError as exc:
            return f"EPOS {r['product_id']}: {exc.code}"
        if rule.target_qbo_item_id != r["item_id"] or str(rule.purchase_multiplier) != r["multiplier"]:
            return (f"EPOS {r['product_id']} now maps to {rule.target_qbo_item_id} x{rule.purchase_multiplier} "
                    f"(plan: {r['item_id']} x{r['multiplier']})")
    return ""


def verify_bill(bill: dict, p: dict) -> list[str]:
    payload = p["payload"]
    problems = []
    if clean((bill.get("VendorRef") or {}).get("value")) != payload["VendorRef"]["value"]:
        problems.append(f"vendor {(bill.get('VendorRef') or {}).get('value')} != {payload['VendorRef']['value']}")
    if clean(bill.get("DocNumber")) != payload["DocNumber"]:
        problems.append(f"DocNumber {bill.get('DocNumber')} != {payload['DocNumber']}")
    if clean(bill.get("TxnDate")) != payload["TxnDate"]:
        problems.append(f"TxnDate {bill.get('TxnDate')} != {payload['TxnDate']}")
    if clean((bill.get("APAccountRef") or {}).get("value")) != AP_ACCOUNT_ID:
        problems.append(f"AP account {(bill.get('APAccountRef') or {}).get('value')} != {AP_ACCOUNT_ID}")
    total = D(bill.get("TotalAmt"), Decimal(0))
    vat_lines = any(ln["ItemBasedExpenseLineDetail"]["TaxCodeRef"]["value"] != TAX_NO_VAT for ln in payload["Line"])
    if abs(total - Decimal(p["bill_total"])) > (TOL if vat_lines else Decimal("0.01")):
        problems.append(f"TotalAmt {total} != {p['bill_total']}")
    got = [ln for ln in bill.get("Line") or [] if ln.get("DetailType") == "ItemBasedExpenseLineDetail"]
    if len(got) != len(payload["Line"]) or len(bill.get("Line") or []) != len(payload["Line"]):
        problems.append(f"{len(bill.get('Line') or [])} lines != {len(payload['Line'])}")
    for want, have in zip(payload["Line"], got):
        w, h = want["ItemBasedExpenseLineDetail"], have.get("ItemBasedExpenseLineDetail") or {}
        if clean((h.get("ItemRef") or {}).get("value")) != w["ItemRef"]["value"]:
            problems.append(f"line item {(h.get('ItemRef') or {}).get('value')} != {w['ItemRef']['value']}")
        if abs(D(h.get("Qty"), Decimal(0)) - Decimal(str(w["Qty"]))) > Decimal("0.00001"):
            problems.append(f"line qty {h.get('Qty')} != {w['Qty']}")
        if abs(D(have.get("Amount"), Decimal(0)) - Decimal(str(want["Amount"]))) > Decimal("0.01"):
            problems.append(f"line amount {have.get('Amount')} != {want['Amount']}")
    if abs(D(bill.get("Balance"), Decimal(0)) - total) > Decimal("0.01"):
        problems.append(f"Balance {bill.get('Balance')} != TotalAmt (bill should be unpaid)")
    return problems


def live_recheck(client: QBOClient, p: dict, registry) -> tuple[str, str, dict | None]:
    """("ok"|"adopt"|"hold"|"stop", detail, existing bill) immediately before a POST."""
    payload = p["payload"]
    doc = payload["DocNumber"]
    existing = as_list(client.query(f"select * from Bill where DocNumber = '{qbo_escape(doc)}'").get("Bill"))
    if existing:
        if len(existing) == 1 and not verify_bill(existing[0], p):
            return "adopt", f"Bill {existing[0]['Id']} already exists with this DocNumber and matches", existing[0]
        return "stop", f"Bill(s) {[b['Id'] for b in existing]} carry {doc} but do not match the payload", None
    d0 = date.fromisoformat(payload["TxnDate"])
    vid = payload["VendorRef"]["value"]
    near = as_list(client.query(
        f"select * from Bill where TxnDate >= '{(d0 - timedelta(days=5)).isoformat()}' and "
        f"TxnDate <= '{(d0 + timedelta(days=5)).isoformat()}' maxresults 1000").get("Bill"))
    dups = manual_duplicates(near, vendor_id=vid, txn_date=payload["TxnDate"],
                             totals=[Decimal(p["epos_total_inc"]), Decimal(p["epos_total_ex"])],
                             item_ids=set(p["item_ids"]), doc=doc)
    if dups:
        return "hold", "; ".join(dups), None
    vendor = client.get_json(f"/vendor/{vid}").get("Vendor") or {}
    if vendor.get("Active") is False:
        return "hold", f"vendor {vid} is inactive", None
    rules = {r.target_qbo_item_id: r for r in registry.rules if r.target_qbo_item_id}
    for item_id in sorted(set(p["item_ids"])):
        item = client.get_json(f"/item/{item_id}").get("Item")
        rule = rules.get(item_id)
        if rule is None:
            return "hold", f"item {item_id} is no longer an approved mapping target", None
        problems = item_problems(item, rule)
        if problems:
            return "hold", f"item {item_id}: {'; '.join(problems)}", None
    prefs = client.get_json("/preferences").get("Preferences", {})
    close = clean((prefs.get("AccountingInfoPrefs") or {}).get("BookCloseDate"))
    if close and payload["TxnDate"] <= close:
        return "hold", f"TxnDate inside the closed period ({close})", None
    return "ok", "", None


def advance_cursor(plan: dict, results: dict, review: dict, approval_ref: str, plan_dir: Path) -> str | None:
    """Move the cursor to the last closed business day whose POs are all done."""
    entries = plan["entries"]
    done = set()
    for e in entries:
        ref = e["po"]["ref"]
        if e["status"] in {"SKIP", "EXCLUDED"} or results.get(ref, {}).get("status") in DONE_RESULTS:
            done.add(ref)
        elif clean(review.get(ref, {}).get("Approve")).lower() in APPROVE_SKIP:
            done.add(ref)
    start = date.fromisoformat(plan["window"][0])
    limit = date.fromisoformat(plan["closed_through"])
    by_day = defaultdict(list)
    for e in entries:
        by_day[e["po"]["received_date"]].append(e["po"]["ref"])
    last = None
    day = start
    while day <= limit:
        if any(ref not in done for ref in by_day.get(day.isoformat(), [])):
            break
        last = day
        day += timedelta(days=1)
    if last is None:
        return None
    cur = read_cursor()
    if cur.get("last_complete_business_date") and cur["last_complete_business_date"] >= last.isoformat():
        return cur["last_complete_business_date"]
    dump_json(cursor_path(), {"last_complete_business_date": last.isoformat(), "updated_at": now_iso(),
                              "plan_dir": str(plan_dir), "approval_ref": approval_ref})
    return last.isoformat()


def run_post(plan_dir: Path, *, client: QBOClient, registry, approval_ref: str, expect_sha: str,
             review_path: Path | None, auto: bool = False) -> dict:
    """Post approved READY bills. Raises StopRun on any gate failure (nothing posted)."""
    if not clean(approval_ref):
        raise StopRun("post requires --approval-ref (the chat yes reference)")
    summary = json.loads((plan_dir / "summary.json").read_text())
    plan = json.loads((plan_dir / "plan.json").read_text())
    actual = sha256_file(plan_dir / "payloads.jsonl")
    if not clean(expect_sha) or expect_sha != actual or summary.get("payloads_sha256") != actual:
        raise StopRun(f"--expect-sha mismatch: payloads.jsonl is {actual}; re-review the plan")
    if summary.get("realm") != REALM:
        raise StopRun("plan realm mismatch")
    mapping_changed = summary["mapping"]["sha256"] != registry.source_sha256
    payloads = {}
    for line in (plan_dir / "payloads.jsonl").read_text().splitlines():
        if line.strip():
            p = json.loads(line)
            if sha256_text(canonical_json(p["payload"])) != p["payload_sha256"]:
                raise StopRun(f"PO {p['po']}: payload does not match its sha256")
            payloads[p["po"]] = p
    review = {}
    status = {e["po"]["ref"]: e["status"] for e in plan["entries"]}
    if auto:
        if not env_flag(AUTO_ENV) or not clean(os.getenv(AUTO_REF_ENV)):
            raise StopRun(f"automated posting needs {AUTO_ENV}=1 and {AUTO_REF_ENV}")
        cap = Decimal(os.getenv(AUTO_MAX_BILL_ENV, "").strip() or "2000000")
        max_count = int(os.getenv(AUTO_MAX_COUNT_ENV, "").strip() or "20")
        approved = [p for p in payloads.values() if not p["warnings"]]
    else:
        if review_path is None:
            raise StopRun("post requires --review <plan>/review.csv with an Approve column")
        rows = read_csv(review_path)
        if not rows or "Approve" not in rows[0]:
            raise StopRun("review.csv has no Approve column; refusing")
        review = {r["PO"]: r for r in rows}
        approved = []
        for ref, r in review.items():
            if clean(r.get("Approve")).lower() not in APPROVE_YES:
                continue
            p = payloads.get(ref)
            if status.get(ref) != "READY" or p is None:
                raise StopRun(f"PO {ref} is approved but is {status.get(ref)} in the plan; only READY bills post")
            if (r.get("DocNumber") != p["doc_number"] or r.get("Bill Total") != p["bill_total"]
                    or r.get("Payload SHA") != p["payload_sha256"]):
                raise StopRun(f"PO {ref}: review.csv row was edited (DocNumber / total / sha differ from the plan)")
            approved.append(p)
        cap, max_count = None, None
    results_path = plan_dir / "results.csv"
    results = load_results(results_path)
    xinfo = summary.get("exclusions_file") or {}
    exclusions = review_exclusions.load(Path(xinfo["path"])) if xinfo.get("path") else review_exclusions.empty()
    supplier_of = {e["po"]["ref"]: clean(e["po"].get("supplier")) for e in plan["entries"]}
    counts = Counter()
    stop = None
    for p in sorted(approved, key=lambda x: (x["received_date"], x["po"])):
        if results.get(p["po"], {}).get("status") in DONE_RESULTS:
            counts["ALREADY_DONE"] += 1
            continue
        base = {"ts": now_iso(), "PO": p["po"], "DocNumber": p["doc_number"], "payload_sha256": p["payload_sha256"],
                "requestid": p["requestid"], "approval_ref": approval_ref, "mode": "auto" if auto else "review"}
        # exclusions added after the plan win over an approval: never post an excluded PO / supplier
        why = exclusions.describe("bill", p["po"])
        if why:
            counts["EXCLUDED"] += 1
            append_result(results_path, {**base, "status": "RESOLVED", "detail": f"{PO_EXCLUDED_NOTE}: {why}"})
            continue
        why = exclusions.describe("vendor", supplier_of.get(p["po"])) if supplier_of.get(p["po"]) else ""
        if why:
            counts["HELD_LIVE"] += 1
            append_result(results_path, {**base, "status": "HELD_LIVE", "detail": f"supplier excluded: {why}"})
            continue
        if auto and (Decimal(p["bill_total"]) > cap or counts["POSTED"] >= max_count):
            counts["CAPPED"] += 1
            append_result(results_path, {**base, "status": "CAPPED", "Total": p["bill_total"],
                                         "detail": f"auto cap N{cap} / {max_count} bills per run; needs human approval"})
            continue
        drift = mapping_drift(p, registry) if mapping_changed else ""
        if drift:
            verdict, detail, existing = "hold", f"mapping changed since the plan: {drift}; re-run plan", None
        else:
            verdict, detail, existing = live_recheck(client, p, registry)
        if verdict == "stop":
            append_result(results_path, {**base, "status": "STOPPED", "detail": detail})
            stop = detail
            break
        if verdict == "hold":
            counts["HELD_LIVE"] += 1
            append_result(results_path, {**base, "status": "HELD_LIVE", "detail": detail})
            continue
        if verdict == "adopt":
            bill, state = existing, "ADOPTED"
        else:
            resp = client.post_json("/bill", p["payload"], p["requestid"])
            if resp.status_code != 200:
                again = as_list(client.query(
                    f"select * from Bill where DocNumber = '{qbo_escape(p['doc_number'])}'").get("Bill"))
                if len(again) == 1 and not verify_bill(again[0], p):
                    bill, state = again[0], "POSTED"
                else:
                    detail = f"POST /bill failed {resp.status_code}: {resp.text[:400]}"
                    append_result(results_path, {**base, "status": "FAILED", "detail": detail})
                    stop = detail
                    break
            else:
                bill, state = resp.json().get("Bill") or {}, "POSTED"
            if not bill.get("Id"):
                stop = "200 without a Bill Id; re-run to reconcile by DocNumber"
                append_result(results_path, {**base, "status": "STOPPED", "detail": stop})
                break
        live = client.get_json(f"/bill/{bill['Id']}").get("Bill") or {}
        problems = verify_bill(live, p)
        append_result(results_path, {**base, "status": state if not problems else "VERIFY_FAILED",
                                     "Bill Id": live.get("Id"), "SyncToken": live.get("SyncToken"),
                                     "Total": live.get("TotalAmt"), "detail": "; ".join(problems)})
        if problems:
            stop = f"Bill {live.get('Id')} failed verification: {'; '.join(problems)}"
            break
        counts[state] += 1
    results = load_results(results_path)
    cursor = advance_cursor(plan, results, review, approval_ref, plan_dir) if not stop else read_cursor().get(
        "last_complete_business_date")
    out = {"run_at": now_iso(), "approval_ref": approval_ref, "mode": "auto" if auto else "review",
           "approved": len(approved), "counts": dict(counts), "stopped": stop, "cursor": cursor,
           "qbo_requests": client.requests}
    dump_json(plan_dir / f"post_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", out)
    return out


def cmd_post(a) -> int:
    if a.auto:
        if not a.plan_dir:
            raise StopRun("--auto needs --plan-dir")
        plan_dir = Path(a.plan_dir)
        approval_ref = clean(os.getenv(AUTO_REF_ENV))
        expect = a.expect_sha or json.loads((plan_dir / "summary.json").read_text())["payloads_sha256"]
        review_path = None
    else:
        if not a.review:
            raise StopRun("post requires --review <plan>/review.csv")
        review_path = Path(a.review)
        plan_dir = Path(a.plan_dir) if a.plan_dir else review_path.parent
        approval_ref, expect = a.approval_ref, a.expect_sha
    registry = load_registry(Path(a.mapping) if a.mapping else None)
    client = QBOClient.for_company_a(allow_writes=True)
    res = run_post(plan_dir, client=client, registry=registry, approval_ref=approval_ref, expect_sha=expect,
                   review_path=review_path, auto=a.auto)
    print(json.dumps(res, indent=1))
    if not a.no_slack:
        send_slack(f"Akponora bills_sync post ({res['mode']}): {res['counts']}; stopped: {res['stopped'] or 'no'}; "
                   f"cursor {res['cursor']}. Bills are unpaid - pay them in QBO. Folder: {plan_dir}")
    return 2 if res["stopped"] else 0


def cmd_scheduled(a, *, client: QBOClient | None = None, write_client: QBOClient | None = None) -> int:
    """Plan (creating genuinely new vendors when the vendor gates allow), then auto-post when the
    bills gates allow. Writes ``scheduled.json`` (counts, stop reason, waiting) for daily_run."""
    a.create_vendors = True
    out, summary, _ = run_plan(a, client=client, write_client=write_client)
    posted, stopped = None, None
    if env_flag(AUTO_ENV) and clean(os.getenv(AUTO_REF_ENV)) and summary["counts"].get("READY"):
        registry = load_registry(Path(a.mapping) if a.mapping else None)
        wclient = write_client or QBOClient.for_company_a(allow_writes=True)
        res = run_post(out, client=wclient, registry=registry, approval_ref=clean(os.getenv(AUTO_REF_ENV)),
                       expect_sha=summary["payloads_sha256"], review_path=None, auto=True)
        posted, stopped = res["counts"], res["stopped"]
    cash = None
    if env_flag(AUTO_ENV) and clean(os.getenv(AUTO_REF_ENV)) and not stopped:
        # Cash-on-delivery POs: pay the bill from Petty Cash on its own date (bill_payments; owner yes 4 Oct).
        from code_scripts.akponora_ops import bill_payments

        try:
            cash = bill_payments.pay_cash_bills(write_client or QBOClient.for_company_a(allow_writes=True))
        except Exception as exc:  # noqa: BLE001 - a payment problem never undoes or blocks posted bills
            cash = {"enabled": True, "paid": [], "failed": [{"detail": f"{type(exc).__name__}: {exc}"}],
                    "capped": [], "planned": [], "total": "0.00"}
        dump_json(out / "cash_payments.json", cash)
    waiting = summary["counts"].get("HOLD", 0) + summary["counts"].get("READY", 0) - sum(
        (posted or {}).get(k, 0) for k in ("POSTED", "ADOPTED"))
    dump_json(out / "scheduled.json", {"posted": posted or {}, "stopped": stopped, "waiting": waiting,
                                       "auto_post": bool(env_flag(AUTO_ENV) and clean(os.getenv(AUTO_REF_ENV))),
                                       "cash_paid": len((cash or {}).get("paid") or []),
                                       "cash_paid_total": (cash or {}).get("total", "0.00"),
                                       "cash_pay_failed": [f.get("detail", "") for f in (cash or {}).get("failed") or []]})
    if not a.no_slack:
        send_slack(slack_text(summary, out, posted) + (f"\nSTOPPED: {stopped}" if stopped else ""))
    if stopped:
        return 2
    return 3 if waiting else 0


# ---------------------------------------------------------------- vendors-suggest
def history_suppliers(history: Path) -> dict:
    """{normalized supplier: Counter(QBO vendor id)} from a September bills_from_epos_pos build:
    POs matched 1:1 to a QBO bill by amount and date are strong vendor evidence."""
    match = history / "po_bill_match_2026-09.csv"
    snap = history / "evidence" / "qbo_snapshot.json"
    out = defaultdict(Counter)
    if not match.exists() or not snap.exists():
        return out
    bill_vendor = {b["Id"]: (b.get("VendorRef") or {}).get("value") for b in json.loads(snap.read_text())["bills"]}
    for r in read_csv(match):
        if r["status"].startswith("BILLED") and r["qbo_bill_ids"] and r["supplier_from_po_note"]:
            for bid in r["qbo_bill_ids"].split(";"):
                if bill_vendor.get(bid):
                    out[vendor_key(r["supplier_from_po_note"])][bill_vendor[bid]] += 1
    return out


def suggest_vendors(suppliers: dict, vendors: dict, history: dict, mapped: set, bill_counts: Counter) -> list[dict]:
    active = [v for v in vendors.values() if v.get("Active", True)]
    rows = []
    for key, info in sorted(suppliers.items(), key=lambda kv: (-len(kv[1]["refs"]), kv[0])):
        name = info["names"].most_common(1)[0][0]

        ranked = sorted(((vendor_ops.score(name, v, bill_counts), bill_counts.get(v["Id"], 0), v) for v in active),
                        key=lambda t: (-t[0], -t[1]))
        evidence, best, s = "", None, 0.0
        hist = history.get(key)
        if hist:
            vid, n = hist.most_common(1)[0]
            if vid in vendors:
                best, s = vendors[vid], 1.0
                evidence = f"September: {n} EPOS PO(s) with this supplier matched QBO bills of this vendor by amount/date"
        if best is None and ranked:
            s, _, best = ranked[0]
            evidence = f"fuzzy name match {s:.2f}" + (" - CHECK, may be a new vendor" if s < 0.85 else "")
            if s < 0.6:
                best, evidence = None, "no close QBO vendor - create the vendor in QBO (human), then map"
        second = next((f"{v['Id']} {v['DisplayName']} ({sc:.2f})" for sc, _, v in ranked
                       if best is None or v["Id"] != best["Id"]), "")
        rows.append({"EPOS Supplier Id": info["id"], "EPOS Supplier Name": name, "Normalized": key,
                     "PO Count": len(info["refs"]), "PO Refs": " ".join(sorted(info["refs"])[:15]),
                     "Already Mapped": "yes" if key in mapped else "",
                     "Suggested QBO Vendor Id": best["Id"] if best else "",
                     "Suggested QBO Vendor Name": best["DisplayName"] if best else "",
                     "Score": f"{s:.2f}" if best else "", "Evidence": evidence, "Second Choice": second,
                     "Approved By": ""})
    return rows


def cmd_vendors_suggest(a) -> int:
    out = run_dir(TOOL, a.out)
    vpath = Path(a.vendors) if a.vendors else vendor_file()
    ensure_vendor_file(vpath)
    suppliers = defaultdict(lambda: {"names": Counter(), "refs": set(), "id": ""})
    sources = []
    if a.po_list and a.po_details:
        sources.append(load_pos(a.po_list, a.po_details))
    elif (out / "evidence" / "po_list_raw.json").exists() and (out / "evidence" / "po_details.jsonl").exists():
        sources.append(load_pos(out / "evidence" / "po_list_raw.json", out / "evidence" / "po_details.jsonl"))
    history = Path(a.history) if a.history else DEFAULT_HISTORY
    if (history / "evidence" / "po_details.jsonl").exists():
        sources.append(load_pos(history / "evidence" / "po_list_raw.json", history / "evidence" / "po_details.jsonl"))
    for orders, details in sources:
        for o in orders:
            po = build_po(o, details.get(str(o["OrderRef"])))
            key = vendor_key(po["supplier"])
            if not key:
                continue
            suppliers[key]["names"][po["supplier"]] += 1
            suppliers[key]["refs"].add(po["ref"])
            suppliers[key]["id"] = suppliers[key]["id"] or po["supplier_id"]
    client = QBOClient.for_company_a(allow_writes=False)
    vendors = {v["Id"]: v for v in client.query_all("select * from Vendor where Active in (true,false)", "Vendor")}
    counted = client.query_all("select * from Bill where TxnDate >= '2026-06-01'", "Bill")
    bill_counts = Counter((b.get("VendorRef") or {}).get("value") for b in counted)
    _, by_name, _ = load_vendor_map(read_csv(vpath))
    rows = suggest_vendors(suppliers, vendors, history_suppliers(history), set(by_name), bill_counts)
    write_csv(out / "vendors_suggest.csv", rows, SUGGEST_COLS)
    dump_json(out / "vendors_suggest_summary.json", {
        "suppliers": len(rows), "with_september_evidence": sum(1 for r in rows if r["Evidence"].startswith("September")),
        "fuzzy": sum(1 for r in rows if r["Evidence"].startswith("fuzzy")),
        "no_match": sum(1 for r in rows if not r["Suggested QBO Vendor Id"]),
        "qbo_vendors": len(vendors), "vendors_file": str(vpath), "qbo_requests": client.requests,
        "how_to_approve": "copy approved rows (EPOS Supplier Id, EPOS Supplier Name, QBO Vendor Id, QBO Vendor "
                          "Name, Approved By) into the vendors file; never auto-create vendors"})
    print(f"{len(rows)} supplier(s) -> {out / 'vendors_suggest.csv'}")
    return 0


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    setup_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--out", default=None, help="evidence folder (default outputs/bills_sync_<UTC>/)")
        p.add_argument("--mapping", default=None, help="override the installed approved mapping")
        p.add_argument("--vendors", default=None, help="override STATE_ROOT/mappings/company_a/vendors.csv")
        p.add_argument("--po-list", default=None, help="offline: po_list_raw.json instead of a live EPOS capture")
        p.add_argument("--po-details", default=None, help="offline: po_details.jsonl")
        p.add_argument("--no-slack", action="store_true")

    for name in ("plan", "scheduled"):
        p = sub.add_parser(name, help="READ-ONLY plan" if name == "plan" else "plan + gated auto post (cron)")
        common(p)
        p.add_argument("--from", dest="date_from", default=None, help="received business date from (YYYY-MM-DD)")
        p.add_argument("--to", dest="date_to", default=None, help="received business date to (default today)")
        p.add_argument("--tax-mode", choices=("gross", "split"), default="gross")
        p.add_argument("--dup-days", type=int, default=14, help="duplicate-receipt lookback days (default 14)")
        p.add_argument("--dup-min-value", default="50000", help="duplicate hold threshold, inc-tax (default 50000)")
        p.add_argument("--history", default=None, help=f"September build folder for vendor evidence (default {DEFAULT_HISTORY})")
    p = sub.add_parser("post", help="WRITES: post approved READY bills")
    p.add_argument("--review", default=None, help="<plan>/review.csv with Approve=yes rows")
    p.add_argument("--plan-dir", default=None, help="plan folder (default: the review.csv folder)")
    p.add_argument("--approval-ref", default="")
    p.add_argument("--expect-sha", default="", help="payloads_sha256 from the plan summary.json")
    p.add_argument("--auto", action="store_true", help=f"no Approve column; needs {AUTO_ENV}=1")
    p.add_argument("--mapping", default=None)
    p.add_argument("--no-slack", action="store_true")
    p = sub.add_parser("vendors-suggest", help="READ-ONLY vendor suggestions for human approval")
    common(p)
    p.add_argument("--history", default=None, help=f"September build folder (default {DEFAULT_HISTORY})")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in sub.choices and args[0] not in ("-h", "--help"):
        args = ["plan", *args]
    a = ap.parse_args(args)
    try:
        if a.cmd == "plan":
            return cmd_plan(a)
        if a.cmd == "scheduled":
            return cmd_scheduled(a)
        if a.cmd == "post":
            return cmd_post(a)
        return cmd_vendors_suggest(a)
    except StopRun as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
