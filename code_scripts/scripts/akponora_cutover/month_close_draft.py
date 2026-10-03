#!/usr/bin/env python3
"""AKPONORA (company_a) month-end inventory close (option 1) -- DRY-RUN DRAFT.

STRICTLY READ-ONLY. This script never POSTs, PUTs or DELETEs anything to QBO
or EPOS. The only network calls are QBO GET queries (plus, if the stored
access token has expired, the standard OAuth refresh done by token_manager).
Journal "payloads" are written to disk as drafts for human approval.

Closing value = EPOS stock report zero-floor value (TotalCost where TotalStock > 0).
Movement journal = closing - opening (the IA reset value), Dr/Cr Inventory Asset 77
vs Cost of sales 76, DocNumber COGS-YYYY-MM dated the month end. Also drafts the
create-night IA offset (300150) for the first day of the next month.

Example (the real 30 Sep pack):
  python -m code_scripts.scripts.akponora_cutover.month_close_draft --month 2026-09 \
      --stock-report "<30 Sep StockReport CSV>" --products-dir "<30 Sep Products dir>" \
      --bookkeeping "<BookKeeping CSV covering 16-30 Sep>" \
      --closing-label 2026-09-30 --out-tag 2026-09-30
Defaults reproduce the 25 Sep interim draft (outputs/nora_gaps_2026-09-25/sep_close_draft_*).
Output JSON keys keep their original (September) names for compatibility.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import DEFAULT_EVIDENCE_ROOT, ReadOnlyQBO as _ReadOnlyQBO, resolve_out

TOOL = "month_close_draft"
MISC = DEFAULT_EVIDENCE_ROOT  # overridden by --evidence-root

# ---------------------------------------------------------------- defaults
OPENING_SNAPSHOT_AT = "2026-09-16 21:08"

# Defaults are the September 2026 close; main() re-derives them from --month etc.
# Opening = IA reset on 16 Sep (JE 75153/75154) to EPOS 16 Sep zero-floor.
OPENING_VALUE = Decimal("142028049.94")
# Prior option-1 close (31 Aug EPOS Stock History) -- disclosure only.
AUG_CLOSE_VALUE = Decimal("135620665.76")

MONTH_START = "2026-09-01"
MONTH_END = "2026-09-30"
JOURNAL_DATE = "2026-09-30"
JOURNAL_DOC = "COGS-2026-09"
OFFSET_DOC = "INV-OPEN-OFFSET-2026-10-01"
OFFSET_DATE = "2026-10-01"
MONTH_LABEL, CLOSE_LABEL, RESET_LABEL = "Sep 2026", "30 Sep", "16 Sep"

IA_ID, IA_NAME = "77", "Inventory Asset"
COGS_ID, COGS_NAME = "76", "200000 - Cost of sales"
EQUITY_ID, EQUITY_NAME = "86", "300150 - Historical Inventory Valuation Correction"
TAX_CODE = "7"  # same as Jul/Aug option-1 journals
INV_FAMILY_IDS = {
    "1150040008": "120000 - Inventory",
    "1150040011": "120100 - Grocery",
    "1150040012": "120200 - Drinks",
    "1150040029": "120201 - Alcoholic Drinks",
    "1150040030": "120202 - Non-Alcoholic Drinks",
    "1150040013": "120300 - Non - Food Items",
}
INV_FAMILY_LEAVES = ("1150040011", "1150040029", "1150040030", "1150040013")
COGS_PURCHASE_NUMS = ("200100", "200201", "200202", "200300")
KNOWN_BREACH_IDS = {"75164"}
# INV-CONS/INV-EQ were created 2026-09-17T04:07 +0100 (TxnDate 16 Sep). Anything
# created after this date hit IA/120xxx after the reset.
RESET_CREATED_BY = "2026-09-16"
GL_FROM = "2026-06-01"  # GL report window start (older start dates truncate)

CENT = Decimal("0.01")


def money(value: object) -> Decimal:
    text = str(value if value is not None else "").strip().replace("₦", "").replace(",", "")
    if not text or text == "-":
        return Decimal("0")
    neg = text.startswith("(") and text.endswith(")")
    try:
        amt = Decimal(text.strip("()"))
    except InvalidOperation:
        return Decimal("0")
    return -amt if neg else amt


def d2(v: Decimal) -> str:
    return str(v.quantize(CENT))


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- EPOS
def read_stock(path: Path) -> tuple[list[dict], Decimal | None]:
    rows, footer = [], None
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("Name") or "").strip().lower().startswith("total:"):
                footer = money(row.get("TotalCost"))
                continue
            rows.append(row)
    return rows, footer


def value_stock(path: Path) -> dict:
    """Zero-floor valuation, same basis as 16 Sep (TotalCost where TotalStock>0)."""
    rows, footer = read_stock(path)
    pos = neg = Decimal("0")
    pos_n = neg_n = zero_n = 0
    zero_cost, calc_diff = [], []
    for r in rows:
        qty = money(r.get("TotalStock") or r.get("MeasuredCurrentStock"))
        tc = money(r.get("TotalCost"))
        cp = money(r.get("MeasuredCostPrice"))
        if abs(qty * cp - tc) > Decimal("0.05"):
            calc_diff.append({"name": r["Name"], "qty": str(qty), "cost": str(cp), "total_cost": str(tc)})
        if qty < 0:
            neg_n += 1
            neg += tc
            continue
        if qty == 0:
            zero_n += 1
            continue
        pos_n += 1
        if tc <= 0:
            zero_cost.append({"name": r["Name"], "qty": str(qty), "cost": str(cp),
                              "category": r.get("CategoryName", "")})
        pos += max(tc, Decimal("0"))
    raw_sum = sum((money(r.get("TotalCost")) for r in rows), Decimal("0"))
    return {
        "file": str(path),
        "sha256": sha256(path),
        "rows": len(rows),
        "footer_total_cost": d2(footer) if footer is not None else None,
        "sum_row_total_cost": d2(raw_sum),
        "footer_matches_rows": footer is not None and abs(footer - raw_sum) < CENT,
        "positive_qty_rows": pos_n,
        "zero_qty_rows": zero_n,
        "negative_qty_rows": neg_n,
        "negative_row_value": d2(neg),
        "zero_floor_value": d2(pos),
        "positive_qty_zero_cost_rows": len(zero_cost),
        "positive_qty_zero_cost": zero_cost,
        "rows_where_qty_x_cost_ne_totalcost": len(calc_diff),
        "_rows": rows,
    }


def load_products(products_dir: Path) -> dict[str, list[dict]]:
    by_name: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(products_dir.glob("*.csv")):
        with f.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh, delimiter=";"):
                by_name[norm(row.get("Name", ""))].append(row)
    return by_name


def cost_basis_check(stock_rows: list[dict], products: dict[str, list[dict]]) -> dict:
    """Compare stock MeasuredCostPrice with product CostPriceExTax / IncTax."""
    counts = defaultdict(int)
    neither, missing, ambiguous = [], [], []
    inc_value = Decimal("0")          # zero-floor value if priced at inclusive cost
    exprod_value = Decimal("0")       # zero-floor value if priced at product ex-tax cost
    neither_pos_value_stock = Decimal("0")
    neither_pos_value_ex = Decimal("0")
    for r in stock_rows:
        qty = money(r.get("TotalStock"))
        tc = money(r.get("TotalCost"))
        cp = money(r.get("MeasuredCostPrice"))
        matches = products.get(norm(r["Name"]), [])
        if not matches:
            missing.append(r["Name"])
            counts["missing_from_products"] += 1
            if qty > 0:
                inc_value += max(tc, Decimal("0"))
                exprod_value += max(tc, Decimal("0"))
            continue
        if len(matches) > 1:
            ambiguous.append(r["Name"])
            counts["ambiguous_name"] += 1
            if qty > 0:
                inc_value += max(tc, Decimal("0"))
                exprod_value += max(tc, Decimal("0"))
            continue
        p = matches[0]
        ex, inc = money(p.get("CostPriceExTax")), money(p.get("CostPriceIncTax"))
        m_ex, m_inc = abs(cp - ex) <= CENT, abs(cp - inc) <= CENT
        key = "both" if (m_ex and m_inc) else "ex_tax_only" if m_ex else "inc_tax_only" if m_inc else "neither"
        counts[key] += 1
        if qty > 0:
            inc_value += max(qty * inc, Decimal("0"))
            exprod_value += max(qty * ex, Decimal("0"))
        if key == "neither":
            item = {"name": r["Name"], "qty": str(qty), "stock_cost": str(cp),
                    "product_ex": str(ex), "product_inc": str(inc),
                    "tax_group": p.get("SalePriceTaxGroupId", ""),
                    "value_at_stock_cost": d2(max(tc, Decimal("0")) if qty > 0 else Decimal("0")),
                    "value_at_product_ex": d2(max(qty * ex, Decimal("0")) if qty > 0 else Decimal("0"))}
            neither.append(item)
            if qty > 0:
                neither_pos_value_stock += max(tc, Decimal("0"))
                neither_pos_value_ex += max(qty * ex, Decimal("0"))
    neither.sort(key=lambda x: abs(money(x["value_at_stock_cost"]) - money(x["value_at_product_ex"])), reverse=True)
    return {
        "counts": dict(counts),
        "neither_rows_with_positive_qty": sum(1 for x in neither if money(x["qty"]) > 0),
        "neither_positive_value_at_stock_cost": d2(neither_pos_value_stock),
        "neither_positive_value_at_product_ex": d2(neither_pos_value_ex),
        "sensitivity_zero_floor_at_product_ex_cost": d2(exprod_value),
        "sensitivity_zero_floor_at_product_inc_cost": d2(inc_value),
        "neither_top25_by_value_gap": neither[:25],
        "missing_from_products": missing,
        "ambiguous_names": ambiguous,
        "_neither": neither,
    }


def epos_sales_cost_between(bookkeeping: Path, start: str, end: str) -> dict:
    t0 = datetime.strptime(start, "%Y-%m-%d %H:%M")
    t1 = datetime.strptime(end, "%Y-%m-%d %H:%M")
    gross = net = cost = Decimal("0")
    lines = 0
    first = last = None
    with bookkeeping.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                ts = datetime.strptime(r["Date/Time"], "%d/%m/%Y %H:%M:%S")
            except (KeyError, ValueError):
                continue
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
            if t0 <= ts < t1:
                lines += 1
                gross += money(r.get("TOTAL Sales"))
                net += money(r.get("NET Sales"))
                cost += money(r.get("Cost Price"))
    return {"file": str(bookkeeping), "window": [start, end], "file_first": str(first),
            "file_last": str(last), "lines": lines, "gross": d2(gross), "net": d2(net),
            "epos_cost_of_sales": d2(cost)}


# ---------------------------------------------------------------- QBO (GET only)
class ReadOnlyQBO(_ReadOnlyQBO):
    """GET-only client (shared helper); token never printed."""


def gl_created_after(q: ReadOnlyQBO, account_id: str) -> dict:
    """GeneralLedger rows on one account whose Create Date is after the reset."""
    body = q.get(f"reports/GeneralLedger?account={account_id}&start_date={GL_FROM}"
                 f"&end_date={MONTH_END}&columns=tx_date,txn_type,doc_num,name,memo,"
                 f"subt_nat_amount,create_date")
    cols = [c.get("ColTitle") or c.get("ColType") for c in body.get("Columns", {}).get("Column", [])]
    flat: list[dict] = []

    def walk(rows):
        for r in rows or []:
            if r.get("ColData"):
                flat.append(dict(zip(cols, [c.get("value") for c in r["ColData"]])))
            if r.get("Rows"):
                walk(r["Rows"].get("Row"))

    walk(body.get("Rows", {}).get("Row"))
    agg = defaultdict(lambda: [0, Decimal("0")])
    names = defaultdict(set)
    total = Decimal("0")
    for d in flat:
        cd = (d.get("Create Date") or "")[:10]
        if not cd or cd <= RESET_CREATED_BY or str(d.get("No.") or "").startswith("INV-CONS"):
            continue  # the reset JE itself is not post-reset activity
        amt = money(d.get("Amount"))
        key = f"{d.get('Transaction Type')}|txn {str(d.get('Date'))[:7]}|created {cd}"
        agg[key][0] += 1
        agg[key][1] += amt
        names[d.get("Transaction Type")].add(d.get("Name") or "")
        total += amt
    return {"account_id": account_id, "name": INV_FAMILY_IDS[account_id], "gl_rows_read": len(flat),
            "created_after_reset_total": d2(total),
            "by_type": {k: {"n": v[0], "amount": d2(v[1])} for k, v in sorted(agg.items())},
            "counterparties": {k: sorted(v)[:10] for k, v in names.items()}}


def qbo_section(q: ReadOnlyQBO) -> dict:
    accounts = {a["Id"]: a for a in q.query_all("select * from Account", "Account")}

    def acct_num(aid: str) -> str:
        a = accounts.get(aid, {})
        num = (a.get("AcctNum") or "").strip()
        if num:
            return num
        m = re.match(r"^(\d{6})", a.get("Name", ""))
        return m.group(1) if m else ""

    def bal(aid: str) -> dict:
        a = accounts.get(aid, {})
        return {"id": aid, "name": a.get("FullyQualifiedName") or a.get("Name"),
                "acct_num": acct_num(aid), "active": a.get("Active"),
                "current_balance": d2(money(a.get("CurrentBalance"))),
                "current_balance_with_sub": d2(money(a.get("CurrentBalanceWithSubAccounts")))}

    cogs_purchase_ids = {aid for aid in accounts if acct_num(aid) in COGS_PURCHASE_NUMS}
    where = f"TxnDate >= '{MONTH_START}' and TxnDate <= '{MONTH_END}'"
    txns = {
        "Bill": q.query_all(f"select * from Bill where {where}", "Bill"),
        "Purchase": q.query_all(f"select * from Purchase where {where}", "Purchase"),
        "VendorCredit": q.query_all(f"select * from VendorCredit where {where}", "VendorCredit"),
    }
    item_ids = sorted({
        (ln.get("ItemBasedExpenseLineDetail") or {}).get("ItemRef", {}).get("value")
        for rows in txns.values() for t in rows for ln in t.get("Line") or []
        if ln.get("DetailType") == "ItemBasedExpenseLineDetail"
    } - {None})
    items: dict[str, dict] = {}
    for i in range(0, len(item_ids), 100):
        chunk = ",".join(f"'{x}'" for x in item_ids[i:i + 100])
        for it in q.query_all(f"select * from Item where Id in ({chunk})", "Item"):
            items[it["Id"]] = it

    def classify(aid: str) -> str:
        if aid == IA_ID or aid in INV_FAMILY_IDS:
            return "BREACH_inventory_account"
        if aid in cogs_purchase_ids:
            return f"COGS_purchase_{acct_num(aid)}"
        return f"other_{acct_num(aid) or aid}"

    lines, totals = [], defaultdict(lambda: Decimal("0"))
    by_bucket_docs = defaultdict(set)
    for kind, rows in txns.items():
        sign = Decimal("-1") if kind == "VendorCredit" else Decimal("1")
        for t in rows:
            for ln in t.get("Line") or []:
                dt = ln.get("DetailType")
                amt = money(ln.get("Amount")) * sign
                item = None
                if dt == "AccountBasedExpenseLineDetail":
                    aid = ln["AccountBasedExpenseLineDetail"].get("AccountRef", {}).get("value", "")
                    bucket = classify(aid)
                elif dt == "ItemBasedExpenseLineDetail":
                    iid = ln["ItemBasedExpenseLineDetail"].get("ItemRef", {}).get("value", "")
                    item = items.get(iid, {})
                    if item.get("Type") == "Inventory":
                        aid = (item.get("AssetAccountRef") or {}).get("value", "")
                        bucket = "BREACH_inventory_item"
                    else:
                        aid = (item.get("ExpenseAccountRef") or {}).get("value", "")
                        bucket = classify(aid)
                else:
                    continue
                totals[bucket] += amt
                by_bucket_docs[bucket].add(f"{kind}:{t['Id']}")
                vendor = (t.get("VendorRef") or t.get("EntityRef") or {}).get("name", "")
                lines.append({
                    "kind": kind, "id": t["Id"], "doc_number": t.get("DocNumber", ""),
                    "txn_date": t.get("TxnDate"), "vendor": vendor, "bucket": bucket,
                    "account_id": aid, "account": bal(aid)["name"] if aid else "",
                    "item_id": (item or {}).get("Id", ""), "item_name": (item or {}).get("Name", ""),
                    "item_type": (item or {}).get("Type", ""),
                    "item_created": ((item or {}).get("MetaData") or {}).get("CreateTime", ""),
                    "amount": d2(amt), "description": (ln.get("Description") or "")[:120],
                    "created": (t.get("MetaData") or {}).get("CreateTime", ""),
                })
    lines.sort(key=lambda x: (x["bucket"], x["txn_date"], x["id"]))
    split = defaultdict(lambda: Decimal("0"))
    for ln in lines:
        half = (f"txn_01-{RESET_CREATED_BY[8:]}" if ln["txn_date"] <= RESET_CREATED_BY
                else f"txn_{int(RESET_CREATED_BY[8:]) + 1:02d}-{MONTH_END[8:]}")
        made = "created_pre_reset" if ln["created"][:10] <= RESET_CREATED_BY else "created_post_reset"
        split[f"{ln['bucket']}|{half}|{made}"] += money(ln["amount"])

    # Journals in the month touching IA / 120xxx / 300150 (context only).
    jes = q.query_all(f"select * from JournalEntry where {where}", "JournalEntry")
    watch = {IA_ID, EQUITY_ID, COGS_ID, *INV_FAMILY_IDS}
    je_hits = []
    for je in jes:
        hit = [ln for ln in je.get("Line") or []
               if (ln.get("JournalEntryLineDetail") or {}).get("AccountRef", {}).get("value") in watch]
        if hit:
            je_hits.append({"id": je["Id"], "doc": je.get("DocNumber"), "date": je.get("TxnDate"),
                            "lines": [{"acct": l["JournalEntryLineDetail"]["AccountRef"].get("name"),
                                       "type": l["JournalEntryLineDetail"].get("PostingType"),
                                       "amount": d2(money(l.get("Amount")))} for l in hit]})
    # Leaf accounts only: a GL report on a parent also returns its sub-accounts.
    family_post_reset = {aid: gl_created_after(q, aid) for aid in INV_FAMILY_LEAVES}
    existing_doc = q.query_all(f"select * from JournalEntry where DocNumber = '{JOURNAL_DOC}'", "JournalEntry")

    return {
        "read_only": True,
        "window": [MONTH_START, MONTH_END],
        "balances": {
            "inventory_asset_77": bal(IA_ID),
            "cogs_76": bal(COGS_ID),
            "equity_300150_86": bal(EQUITY_ID),
            "inv_family": [bal(a) for a in INV_FAMILY_IDS],
            "cogs_purchase_accounts": [bal(a) for a in sorted(cogs_purchase_ids)],
        },
        "counts": {k: len(v) for k, v in txns.items()},
        "totals_by_bucket": {k: d2(v) for k, v in sorted(totals.items())},
        "totals_by_bucket_half": {k: d2(v) for k, v in sorted(split.items())},
        "docs_by_bucket": {k: sorted(v) for k, v in by_bucket_docs.items()},
        "known_breach_ids_seen": sorted(i for i in KNOWN_BREACH_IDS
                                        if any(ln["id"] == i for ln in lines)),
        "month_journals_touching_ia_family_equity_cogs": je_hits,
        "inv_family_postings_created_after_reset": family_post_reset,
        "existing_sep_cogs_journal": [e.get("Id") for e in existing_doc],
        "_lines": lines,
    }


# ---------------------------------------------------------------- drafts
def je_line(desc: str, amount: Decimal, posting: str, acct_id: str, acct_name: str) -> dict:
    return {"Description": desc, "Amount": float(amount), "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {"PostingType": posting,
                                       "AccountRef": {"value": acct_id, "name": acct_name},
                                       "TaxCodeRef": {"value": TAX_CODE},
                                       "TaxApplicableOn": "Purchase", "TaxAmount": 0.0}}


def draft_movement(opening: Decimal, closing: Decimal, label: str) -> dict:
    mv = closing - opening
    amt = abs(mv)
    if mv >= 0:
        dr = je_line(f"Capitalize {MONTH_LABEL} unsold purchases (EPOS stock increase to {label})", amt, "Debit", IA_ID, IA_NAME)
        cr = je_line(f"Reduce {MONTH_LABEL} cost of sales for stock still on hand", amt, "Credit", COGS_ID, COGS_NAME)
    else:
        dr = je_line(f"{MONTH_LABEL} cost of sales: EPOS stock decrease", amt, "Debit", COGS_ID, COGS_NAME)
        cr = je_line(f"Reduce Inventory Asset to EPOS {label}", amt, "Credit", IA_ID, IA_NAME)
    return {
        "status": "DRAFT_NOT_POSTED -- requires chat yes; interim proxy" if label != MONTH_END else "DRAFT_NOT_POSTED -- requires chat yes",
        "formula": f"closing (EPOS zero-floor) - opening ({RESET_LABEL} IA reset)",
        "opening": d2(opening), "closing": d2(closing), "movement": d2(mv),
        "payload_draft": {
            "TxnDate": JOURNAL_DATE, "DocNumber": JOURNAL_DOC,
            "PrivateNote": (f"AKPONORA {MONTH_LABEL} inventory movement only (option 1). Purchases on "
                            f"200100/200201/200202/200300. EPOS {label} zero-floor {d2(closing)} - "
                            f"{RESET_LABEL} IA reset {d2(opening)} = {d2(mv)}. Negatives valued at zero."),
            "Line": [dr, cr],
        },
    }


def draft_offset(ia_before_create: Decimal, create_increase: Decimal, v_opening: Decimal) -> dict:
    off = ia_before_create + create_increase - v_opening
    amt = abs(off)
    if off >= 0:
        lines = [je_line("Opening equity offset: Oct Inventory create night", amt, "Debit", EQUITY_ID, EQUITY_NAME),
                 je_line(f"Remove pre-create IA so IA = {CLOSE_LABEL} EPOS only", amt, "Credit", IA_ID, IA_NAME)]
    else:
        lines = [je_line(f"Top-up IA to {CLOSE_LABEL} EPOS", amt, "Debit", IA_ID, IA_NAME),
                 je_line("Opening equity offset (reverse)", amt, "Credit", EQUITY_ID, EQUITY_NAME)]
    return {
        "status": "DRAFT_NOT_POSTED -- recompute LIVE on create night; chat yes",
        "formula": "credit IA / debit 300150 = B + C - V (runbook)",
        "B_ia_before_create": d2(ia_before_create), "C_create_increase": d2(create_increase),
        "V_approved_opening": d2(v_opening), "offset": d2(off), "ia_after": d2(v_opening),
        "payload_draft": {"TxnDate": OFFSET_DATE, "DocNumber": OFFSET_DOC, "Line": lines},
    }


# ---------------------------------------------------------------- main
def _snap_label(ts: str) -> str:
    """'2026-09-16 21:08' -> '16 Sep 21:08'."""
    t = datetime.strptime(ts, "%Y-%m-%d %H:%M")
    return f"{t.day} {t.strftime('%b %H:%M')}"


def configure(month: str, reset_date: str, opening_snapshot_at: str, gl_from: str) -> None:
    """Derive the month window, journal DocNumbers and labels (module globals) from --month."""
    global MONTH_START, MONTH_END, JOURNAL_DATE, JOURNAL_DOC, OFFSET_DOC, OFFSET_DATE
    global MONTH_LABEL, CLOSE_LABEL, RESET_LABEL, RESET_CREATED_BY, OPENING_SNAPSHOT_AT, GL_FROM
    first = datetime.strptime(month + "-01", "%Y-%m-%d").date()
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = nxt - timedelta(days=1)
    MONTH_START, MONTH_END = first.isoformat(), last.isoformat()
    JOURNAL_DATE, JOURNAL_DOC = MONTH_END, f"COGS-{month}"
    OFFSET_DATE = nxt.isoformat()
    OFFSET_DOC = f"INV-OPEN-OFFSET-{OFFSET_DATE}"
    MONTH_LABEL = first.strftime("%b %Y")
    CLOSE_LABEL = f"{last.day} {last.strftime('%b')}"
    rd = datetime.strptime(reset_date, "%Y-%m-%d").date()
    RESET_LABEL = f"{rd.day} {rd.strftime('%b')}"
    RESET_CREATED_BY, OPENING_SNAPSHOT_AT, GL_FROM = reset_date, opening_snapshot_at, gl_from


def main(argv=None) -> int:
    global MISC
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", default="2026-09", help="YYYY-MM being closed (default %(default)s)")
    ap.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    ap.add_argument("--stock-report", type=Path, default=None,
                    help="closing EPOS StockReport CSV (default <evidence-root>/As of 25th September/Stock Levels/StockReport_2026_09_25_1308.csv)")
    ap.add_argument("--products-dir", type=Path, default=None, help="EPOS Products export dir (; separated CSVs)")
    ap.add_argument("--bookkeeping", type=Path, default=None, help="EPOS BookKeeping CSV covering the month")
    ap.add_argument("--opening-stock-report", type=Path, default=None,
                    help="StockReport at the IA reset (default the 16 Sep 21:08 report)")
    ap.add_argument("--opening-value", type=Decimal, default=OPENING_VALUE, help="IA reset value (default %(default)s)")
    ap.add_argument("--opening-snapshot-at", default=OPENING_SNAPSHOT_AT,
                    help="'YYYY-MM-DD HH:MM' of the opening stock report (default %(default)s)")
    ap.add_argument("--reset-date", default=RESET_CREATED_BY,
                    help="date the IA reset was created; QBO postings created after it are post-reset (default %(default)s)")
    ap.add_argument("--prior-close-value", type=Decimal, default=AUG_CLOSE_VALUE,
                    help="previous month's option-1 close, disclosure only (default %(default)s)")
    ap.add_argument("--gl-from", default=GL_FROM, help="GeneralLedger report start (default %(default)s)")
    ap.add_argument("--closing-label", default="2026-09-25 (interim proxy)")
    ap.add_argument("--closing-snapshot-at", default=None,
                    help="'YYYY-MM-DD HH:MM' of the stock report; parsed from filename if omitted")
    ap.add_argument("--out-tag", default="2026-09-25_interim")
    ap.add_argument("--out", type=Path, default=None, help="output folder (default outputs/month_close_draft_<timestamp>/)")
    ap.add_argument("--no-qbo", action="store_true", help="skip QBO GETs (EPOS-only)")
    args = ap.parse_args(argv)
    configure(args.month, args.reset_date, args.opening_snapshot_at, args.gl_from)
    MISC = args.evidence_root
    args.stock_report = args.stock_report or MISC / "As of 25th September" / "Stock Levels" / "StockReport_2026_09_25_1308.csv"
    args.products_dir = args.products_dir or MISC / "As of 25th September" / "Products"
    args.bookkeeping = args.bookkeeping or MISC / "As of 25th September" / "BookKeeping_2026_09_25_1245.csv"
    args.opening_stock_report = args.opening_stock_report or MISC / "As of 16th September 2026" / "StockReport_2026_09_16_2108.csv"

    opening = value_stock(args.opening_stock_report)
    closing = value_stock(args.stock_report)
    basis = cost_basis_check(closing["_rows"], load_products(args.products_dir))
    opening_ok = money(opening["zero_floor_value"]) == args.opening_value

    snap = args.closing_snapshot_at
    if not snap:
        m = re.search(r"(\d{4})_(\d{2})_(\d{2})_(\d{2})(\d{2})", args.stock_report.name)
        snap = f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}" if m else None
    sales = epos_sales_cost_between(args.bookkeeping, OPENING_SNAPSHOT_AT, snap) if snap else None
    sales_pre = epos_sales_cost_between(args.bookkeeping, f"{MONTH_START} 00:00", OPENING_SNAPSHOT_AT)

    close_v = money(closing["zero_floor_value"])
    movement = draft_movement(args.opening_value, close_v, args.closing_label)
    implied_receipts = None
    if sales:
        implied_receipts = close_v - args.opening_value + money(sales["epos_cost_of_sales"])

    qbo = qbo_section(ReadOnlyQBO()) if not args.no_qbo else None

    ia_now = money(qbo["balances"]["inventory_asset_77"]["current_balance"]) if qbo else args.opening_value
    fam_now = sum((money(b["current_balance"]) for b in qbo["balances"]["inv_family"]), Decimal("0")) if qbo else Decimal("0")
    ia_after_sep = ia_now + (close_v - args.opening_value)
    # Create-night projection: V = closing (placeholder for approved canonical value),
    # C = V if every created item's qty x cost reproduces V exactly.
    offset = draft_offset(ia_after_sep, close_v, close_v)
    offset_if_family_folded = draft_offset(ia_after_sep + fam_now, close_v, close_v)

    # Tests of the option-1 premise, re-evaluated on every run.
    post_cap = post_200 = Decimal("0")
    fam_fold = None
    if qbo:
        for k, v in qbo["totals_by_bucket_half"].items():
            bucket, _half, made = k.split("|")
            if made == "created_post_reset" and bucket.startswith("BREACH"):
                post_cap += money(v)
            if bucket.startswith("COGS_purchase"):
                post_200 += money(v)
        fam_fold = {
            "A_fold_into_sep_cogs": {
                "sep_journal_dr_ia_cr_cogs76": d2(close_v - (ia_now + fam_now)),
                "reclass_dr_120xxx_cr_ia": d2(-fam_now),
                "ia_after": d2(close_v), "family_after": "0.00"},
            "B_fold_to_300150": {
                "sep_journal_dr_ia_cr_cogs76": d2(close_v - args.opening_value),
                "dr_120xxx_cr_300150": d2(-fam_now), "ia_after": d2(ia_after_sep),
                "family_after": "0.00"},
        }
    premise = {
        "sep_bills_on_200xxx_accounts": d2(post_200),
        "bills_capitalised_to_inventory_created_after_reset": d2(post_cap),
        "epos_implied_receipts_since_reset_at_cost": d2(implied_receipts) if implied_receipts is not None else None,
        "option1_valid_only_if": (f"all goods received after the {_snap_label(OPENING_SNAPSHOT_AT)} snapshot are billed to "
                                  "200100/200201/200202/200300 before posting; otherwise use "
                                  "full COGS = opening + capitalised purchases - closing"),
        "full_cogs_alt_if_capitalised": d2(args.opening_value + post_cap - close_v),
    }

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": True, "qbo_writes": 0,
        "closing_label": args.closing_label,
        "opening_stock_check": {k: v for k, v in opening.items() if not k.startswith("_")
                                and k != "positive_qty_zero_cost"},
        "opening_value_used": d2(args.opening_value),
        "opening_recomputes_exactly": opening_ok,
        "closing_stock": {k: v for k, v in closing.items() if not k.startswith("_")},
        "cost_basis": {k: v for k, v in basis.items() if not k.startswith("_")},
        "epos_sales_between_snapshots": sales,
        "epos_sales_1_sep_to_opening_snapshot": sales_pre,
        "implied_receipts_at_cost_between_snapshots": d2(implied_receipts) if implied_receipts is not None else None,
        "sep_movement_journal": movement,
        "disclosure_1_to_16_sep_movement_to_equity": d2(args.opening_value - args.prior_close_value),
        "qbo": {k: v for k, v in qbo.items() if not k.startswith("_")} if qbo else None,
        "projection": {
            "ia_77_now": d2(ia_now), "inv_family_120xxx_now": d2(fam_now),
            "ia_77_after_sep_journal": d2(ia_after_sep),
            "ia_plus_family_after_sep_journal": d2(ia_after_sep + fam_now),
            "target": d2(close_v),
        },
        "option1_premise_check": premise,
        "family_fold_options": fam_fold,
        "create_night_offset": offset,
        "create_night_offset_if_120xxx_folded_into_ia_first": offset_if_family_folded,
    }
    out_dir = resolve_out(args.out, TOOL)
    out_json = out_dir / f"sep_close_draft_{args.out_tag}.json"
    out_json.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    with (out_dir / f"sep_close_cost_mismatches_{args.out_tag}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        fields = ["name", "qty", "stock_cost", "product_ex", "product_inc", "tax_group",
                  "value_at_stock_cost", "value_at_product_ex"]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(basis["_neither"])
    if qbo:
        with (out_dir / f"sep_purchases_{args.out_tag}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(qbo["_lines"][0].keys()) if qbo["_lines"] else ["id"])
            w.writeheader()
            w.writerows(qbo["_lines"])
    brief = {k: result[k] for k in ("opening_value_used", "opening_recomputes_exactly",
                                   "implied_receipts_at_cost_between_snapshots",
                                   "disclosure_1_to_16_sep_movement_to_equity", "projection")}
    brief["closing_zero_floor"] = closing["zero_floor_value"]
    brief["movement"] = movement["movement"]
    brief["basis_counts"] = basis["counts"]
    brief["offset"] = offset["offset"]
    if qbo:
        brief["qbo_totals_by_bucket"] = qbo["totals_by_bucket"]
        brief["qbo_totals_by_bucket_half"] = qbo["totals_by_bucket_half"]
    print(json.dumps(brief, indent=2))
    print(f"Wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
