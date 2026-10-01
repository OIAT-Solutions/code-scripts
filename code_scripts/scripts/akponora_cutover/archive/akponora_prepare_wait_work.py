#!/usr/bin/env python3
"""Prepare backfill dates, EPOS stock values, and journal candidates. Read-only."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

from code_scripts.company_config import get_qbo_api_base_url, load_company_config
from code_scripts.token_manager import get_access_token

REPO = Path(__file__).resolve().parent
OUT = REPO / "outputs" / "akponora_from_2026-08-22"
AUGUST = (
    Path("/Users/marvinmokolo/Developer Projects/OIAT/MISC/AKPONORA Investigation")
    / "COGS Analysis"
    / "As of 22 August 2026"
)
JULY = (
    Path("/Users/marvinmokolo/Developer Projects/OIAT/MISC/AKPONORA Investigation")
    / "COGS Analysis"
    / "As of 23 July 2026"
)

POSTED_ZF = [
    ("COGS-ZF-2026-01", "2026-01-31", Decimal("38874325.04")),
    ("COGS-ZF-2026-02", "2026-02-28", Decimal("67796275.40")),
    ("COGS-ZF-2026-03", "2026-03-31", Decimal("15981556.08")),
    ("COGS-ZF-2026-04", "2026-04-30", Decimal("22056992.69")),
    ("COGS-ZF-2026-05", "2026-05-31", Decimal("96037998.27")),
]
ORIGINAL_SIX_MONTH_TOTAL = Decimal("281069104.71")
MISSING_DAYS_INSIDE = [
    "2026-03-11",
    "2026-03-14",
    "2026-03-31",
    "2026-04-02",
    "2026-04-03",
    "2026-04-04",
    "2026-04-05",
    "2026-04-06",
    "2026-04-07",
    "2026-04-08",
]
LAST_QBO_SALE = date(2026, 6, 11)
TODAY = date(2026, 9, 16)
INVENTORY_ACCOUNT_IDS = {"77", "1150040008"}


def money(value: object) -> Decimal:
    text = str(value or "").strip().replace("₦", "").replace(",", "")
    if not text or text == "-":
        return Decimal("0")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return Decimal("0")
    return -amount if negative else amount


def d2(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def epos_zero_floor_value(stock_report: Path) -> dict[str, object]:
    rows = list(csv.DictReader(stock_report.open(encoding="utf-8-sig")))
    positive = Decimal("0")
    negative_excluded = Decimal("0")
    zero = 0
    pos_n = 0
    neg_n = 0
    missing_cost = 0
    for row in rows:
        qty = money(row.get("TotalStock") or row.get("MeasuredCurrentStock"))
        total_cost = money(row.get("TotalCost"))
        if qty < 0:
            neg_n += 1
            negative_excluded += abs(total_cost)
            continue
        if qty == 0:
            zero += 1
            continue
        pos_n += 1
        if total_cost <= 0:
            missing_cost += 1
        positive += max(total_cost, Decimal("0"))
    return {
        "file": str(stock_report),
        "rows": len(rows),
        "positive_qty_rows": pos_n,
        "zero_qty_rows": zero,
        "negative_qty_rows": neg_n,
        "zero_floor_value": d2(positive),
        "negative_value_excluded": d2(negative_excluded),
        "positive_stock_missing_cost": missing_cost,
    }


def month_days(start: date, end: date) -> list[str]:
    out = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def write_backfill_dates(path: Path) -> dict[str, object]:
    after = month_days(LAST_QBO_SALE + timedelta(days=1), TODAY)
    rows = []
    for day in MISSING_DAYS_INSIDE:
        rows.append({"date": day, "kind": "hole_inside_jan_jun", "status": "missing_in_qbo"})
    for day in after:
        rows.append({"date": day, "kind": "after_pipeline_stop", "status": "missing_in_qbo"})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "kind", "status"])
        writer.writeheader()
        writer.writerows(rows)
    return {
        "holes_inside_jan_jun": MISSING_DAYS_INSIDE,
        "after_last_sale_days": len(after),
        "after_from": after[0],
        "after_to": after[-1],
        "total_days": len(rows),
    }


def write_empty_mapping(path: Path) -> None:
    fields = [
        "Row ID",
        "EPOS Product ID",
        "EPOS Existing SKU",
        "EPOS Name",
        "Pipeline Status",
        "Review Status",
        "Target QBO Item Type",
        "Target QBO Name",
        "Target QBO SKU",
        "Staff Approved Sale Multiplier",
        "Effective Date",
        "Approved By",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.DictWriter(handle, fieldnames=fields).writeheader()


def qbo_client():
    config = load_company_config("company_a")
    token = get_access_token(config.company_key, config.realm_id)
    base = get_qbo_api_base_url(config.qbo_environment)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return config, base, headers


def qbo_query(base: str, realm: str, headers: dict, sql: str) -> dict:
    url = f"{base}/v3/company/{realm}/query?query={quote(sql)}&minorversion=70"
    resp = requests.get(url, headers=headers, timeout=90)
    resp.raise_for_status()
    return resp.json().get("QueryResponse", {})


def qbo_report(base: str, realm: str, headers: dict, name: str, params: dict) -> dict:
    qs = urlencode({"minorversion": "70", **params})
    url = f"{base}/v3/company/{realm}/reports/{name}?{qs}"
    resp = requests.get(url, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()


def walk_amount(rows: list, wanted: tuple[str, ...]) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}

    def walk(rs, depth=0):
        for row in rs or []:
            hdr = (row.get("Header") or {}).get("ColData") or []
            cols = row.get("ColData") or []
            source = hdr or cols
            label = source[0].get("value") if source else ""
            amount = source[-1].get("value") if len(source) > 1 else None
            key = (label or "").strip()
            if key in wanted and amount not in (None, ""):
                found[key] = money(amount)
            child = (row.get("Rows") or {}).get("Row")
            if child and depth < 8:
                walk(child, depth + 1)

    walk(rows)
    return found


def first_entity(payload: dict, entity: str) -> list[dict]:
    rows = payload.get(entity, [])
    if not isinstance(rows, list):
        return [rows] if rows else []
    return rows


def journal_summary(entry: dict) -> dict[str, object]:
    debit = Decimal("0")
    credit = Decimal("0")
    lines = []
    for line in entry.get("Line") or []:
        detail = line.get("JournalEntryLineDetail") or {}
        posting = (detail.get("PostingType") or "").strip()
        amount = money(line.get("Amount"))
        account = (detail.get("AccountRef") or {}).get("name") or ""
        if posting.lower() == "debit":
            debit += amount
        elif posting.lower() == "credit":
            credit += amount
        lines.append(
            {
                "posting": posting,
                "amount": d2(amount),
                "account": account,
                "description": (line.get("Description") or "")[:160],
            }
        )
    return {
        "id": entry.get("Id"),
        "doc_number": entry.get("DocNumber"),
        "txn_date": entry.get("TxnDate"),
        "private_note": (entry.get("PrivateNote") or "")[:200],
        "debit_total": d2(debit),
        "credit_total": d2(credit),
        "lines": lines,
    }


def fetch_journal_by_doc(base, realm, headers, doc_number: str) -> dict[str, object]:
    sql = f"select * from JournalEntry where DocNumber = '{doc_number}'"
    rows = first_entity(qbo_query(base, realm, headers, sql), "JournalEntry")
    if not rows:
        return {"doc_number": doc_number, "found": False}
    summaries = [journal_summary(row) for row in rows]
    return {
        "doc_number": doc_number,
        "found": True,
        "count": len(summaries),
        "entries": summaries,
    }


def fetch_item_by_name(base, realm, headers, name: str) -> dict[str, object]:
    safe = name.replace("'", "''")
    sql = f"select Id, Name, Type, Active, Sku from Item where Name = '{safe}'"
    rows = first_entity(qbo_query(base, realm, headers, sql), "Item")
    if not rows:
        return {"name": name, "exists": False}
    item = rows[0]
    return {
        "name": name,
        "exists": True,
        "id": item.get("Id"),
        "type": item.get("Type"),
        "active": item.get("Active"),
        "sku": item.get("Sku"),
    }


def fetch_account_by_name(base, realm, headers, name: str) -> dict[str, object]:
    safe = name.replace("'", "''")
    sql = f"select Id, Name, AccountType, AccountSubType, CurrentBalance from Account where Name = '{safe}'"
    rows = first_entity(qbo_query(base, realm, headers, sql), "Account")
    if not rows:
        return {"name": name, "exists": False}
    account = rows[0]
    return {
        "name": name,
        "exists": True,
        "id": account.get("Id"),
        "type": account.get("AccountType"),
        "subtype": account.get("AccountSubType"),
        "current_balance": d2(money(account.get("CurrentBalance"))),
    }


def paginate_entities(base, realm, headers, entity: str) -> list[dict]:
    start = 1
    all_rows: list[dict] = []
    while True:
        qr = qbo_query(base, realm, headers, f"select * from {entity} startposition {start} maxresults 100")
        batch = qr.get(entity, [])
        if not isinstance(batch, list):
            batch = [batch] if batch else []
        all_rows.extend(batch)
        if len(batch) < 100:
            break
        start += 100
        if start > 20000:
            break
    return all_rows


def inventory_postings(purchases: list[dict], bills: list[dict]) -> list[dict]:
    out = []
    for kind, rows in (("Purchase", purchases), ("Bill", bills)):
        for txn in rows:
            txn_date = txn.get("TxnDate") or ""
            for line in txn.get("Line") or []:
                detail = line.get("AccountBasedExpenseLineDetail") or {}
                acct = detail.get("AccountRef") or {}
                if acct.get("value") not in INVENTORY_ACCOUNT_IDS and "inventory" not in (
                    acct.get("name") or ""
                ).lower():
                    continue
                out.append(
                    {
                        "kind": kind,
                        "id": txn.get("Id"),
                        "date": txn_date,
                        "month": txn_date[:7],
                        "amount": d2(money(line.get("Amount"))),
                        "account": acct.get("name"),
                        "memo": (txn.get("PrivateNote") or line.get("Description") or "")[:120],
                    }
                )
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    backfill = write_backfill_dates(OUT / "backfill_dates.csv")
    write_empty_mapping(OUT / "akponora_product_conversion_approved_empty.csv")

    july_stock = epos_zero_floor_value(
        JULY / "EPOS" / "Stock Levels" / "StockReport_2026_07_23_1814.csv"
    )
    aug_stock = epos_zero_floor_value(
        AUGUST / "EPOS" / "Stock Levels" / "StockReport_2026_08_22_2330.csv"
    )

    config, base, headers = qbo_client()
    realm = config.realm_id

    posted_live = [fetch_journal_by_doc(base, realm, headers, doc) for doc, _, _ in POSTED_ZF]
    june_live = fetch_journal_by_doc(base, realm, headers, "COGS-ZF-2026-06")
    catch_all_live = fetch_item_by_name(base, realm, headers, "AKP-UNMAPPED-EPOS-SALES")
    inventory_asset = fetch_account_by_name(base, realm, headers, "Inventory Asset")
    cogs_hist = fetch_account_by_name(
        base, realm, headers, "COGS Historical Correction - Jan-Jun 2026"
    )
    inventory_120000 = fetch_account_by_name(base, realm, headers, "120000 - Inventory")

    posted_live_ok = []
    posted_live_mismatch = []
    for expected, live in zip(POSTED_ZF, posted_live):
        doc, txn_date, amount = expected
        check = {
            "doc": doc,
            "expected_date": txn_date,
            "expected_amount": d2(amount),
            "found": live.get("found"),
        }
        if not live.get("found"):
            check["status"] = "MISSING_IN_QBO"
            posted_live_mismatch.append(check)
            continue
        entry = (live.get("entries") or [None])[0] or {}
        check["qbo_id"] = entry.get("id")
        check["qbo_date"] = entry.get("txn_date")
        check["qbo_debit_total"] = entry.get("debit_total")
        check["status"] = "OK"
        if entry.get("txn_date") != txn_date or entry.get("debit_total") != d2(amount):
            check["status"] = "AMOUNT_OR_DATE_MISMATCH"
            posted_live_mismatch.append(check)
        else:
            posted_live_ok.append(check)

    months = [
        ("2026-06", "2026-06-01", "2026-06-30"),
        ("2026-07", "2026-07-01", "2026-07-31"),
        ("2026-08", "2026-08-01", "2026-08-31"),
        ("2026-09", "2026-09-01", "2026-09-16"),
    ]
    pnl_by_month = {}
    for label, start, end in months:
        payload = qbo_report(
            base, realm, headers, "ProfitAndLoss", {"start_date": start, "end_date": end}
        )
        rows = (payload.get("Rows") or {}).get("Row") or []
        amounts = walk_amount(
            rows,
            (
                "Income",
                "Cost of Sales",
                "COGS Historical Correction - Jan-Jun 2026",
                "Cost of sales",
                "200000 - Cost of sales",
            ),
        )
        pnl_by_month[label] = {k: d2(v) for k, v in amounts.items()}
        pnl_by_month[label]["header_end"] = (payload.get("Header") or {}).get("EndPeriod")

    purchases = paginate_entities(base, realm, headers, "Purchase")
    bills = paginate_entities(base, realm, headers, "Bill")
    inv_postings = inventory_postings(purchases, bills)
    by_month = defaultdict(lambda: Decimal("0"))
    for row in inv_postings:
        by_month[row["month"]] += money(row["amount"])

    posted_total = sum((amt for _, _, amt in POSTED_ZF), Decimal("0"))
    june_implied = ORIGINAL_SIX_MONTH_TOTAL - posted_total

    journals = [
        {
            "doc_number": "COGS-ZF-2026-06",
            "txn_date": "2026-06-30",
            "status": "NOT_POSTED",
            "basis": (
                "Implied remainder of the 24 Aug six-month zero-floor schedule "
                f"({d2(ORIGINAL_SIX_MONTH_TOTAL)} total minus posted {d2(posted_total)})."
            ),
            "debit_account": "Inventory Asset",
            "credit_account": "COGS Historical Correction - Jan-Jun 2026",
            "amount": d2(june_implied),
            "needs": "Accountant confirm amount, date, and that June still uses the Jan-Jun schedule.",
        },
        {
            "doc_number": "COGS-EPOS-2026-07",
            "txn_date": "2026-07-31",
            "status": "DRAFT_INCOMPLETE",
            "basis": (
                "EPOS stock value 23 July (zero-floor) is "
                f"{july_stock['zero_floor_value']}. This is a mid-month snapshot, not 30 June or 31 July. "
                "Do not post until opening/closing pair and July purchases are approved."
            ),
            "debit_account": "TBD",
            "credit_account": "TBD",
            "amount": "",
            "epos_snapshot_value": july_stock["zero_floor_value"],
            "qbo_purchases_to_inventory_this_month": d2(by_month.get("2026-07", Decimal("0"))),
            "needs": "Month-end EPOS stock export for 31 July, or explicit OK to use 23 July as close enough.",
        },
        {
            "doc_number": "COGS-EPOS-2026-08",
            "txn_date": "2026-08-31",
            "status": "DRAFT_INCOMPLETE",
            "basis": (
                "EPOS stock value 22 August (zero-floor) is "
                f"{aug_stock['zero_floor_value']}. Snapshot is 22 Aug, not 31 Aug."
            ),
            "debit_account": "TBD",
            "credit_account": "TBD",
            "amount": "",
            "epos_snapshot_value": aug_stock["zero_floor_value"],
            "qbo_purchases_to_inventory_this_month": d2(by_month.get("2026-08", Decimal("0"))),
            "needs": "Month-end EPOS stock export for 31 August, or explicit OK to use 22 August as close enough.",
        },
        {
            "doc_number": "COGS-EPOS-2026-09",
            "txn_date": "2026-09-30",
            "status": "WAIT_FOR_MONTH_END",
            "basis": "September is still open. Pipeline has been off since 12 June.",
            "debit_account": "TBD",
            "credit_account": "TBD",
            "amount": "",
            "needs": "September EPOS stock export after month end, plus backfill decision.",
        },
    ]

    catch_all = {
        "action": "CREATE_NONINVENTORY_WHEN_APPROVED",
        "name": "AKP-UNMAPPED-EPOS-SALES",
        "type": "NonInventory",
        "sku": "AKP-UNMAPPED",
        "exists_in_qbo": bool(catch_all_live.get("exists")),
        "live": catch_all_live,
        "do_not_create_without_chat_approval": True,
        "income_account_hint": "Use the same grocery/sales income account as other till sales.",
    }

    summary = {
        "as_of": TODAY.isoformat(),
        "qbo_last_sales_receipt_date": LAST_QBO_SALE.isoformat(),
        "posted_zero_floor_journals": [
            {"doc": doc, "date": txn_date, "amount": d2(amount)} for doc, txn_date, amount in POSTED_ZF
        ],
        "posted_zero_floor_live": posted_live,
        "posted_zero_floor_live_ok": posted_live_ok,
        "posted_zero_floor_live_mismatch": posted_live_mismatch,
        "june_zero_floor_live": june_live,
        "posted_zero_floor_total": d2(posted_total),
        "implied_june_zero_floor": d2(june_implied),
        "qbo_accounts": {
            "Inventory Asset": inventory_asset,
            "COGS Historical Correction - Jan-Jun 2026": cogs_hist,
            "120000 - Inventory": inventory_120000,
        },
        "epos_stock_july_23": july_stock,
        "epos_stock_august_22": aug_stock,
        "qbo_pnl": pnl_by_month,
        "qbo_purchases_to_inventory_by_month": {k: d2(v) for k, v in sorted(by_month.items())},
        "inventory_posting_count": len(inv_postings),
        "backfill": backfill,
        "journals": journals,
        "catch_all_item": catch_all,
        "blocked_on_staff_csv": False,
        "blocked_on_human_to_post": [
            "Create Non-inventory item AKP-UNMAPPED-EPOS-SALES",
            "Post COGS-ZF-2026-06 for " + d2(june_implied),
            "Confirm offset account for any Inventory Asset true-up (not Shrinkage unless chosen)",
        ],
    }

    (OUT / "open_work_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (OUT / "journal_candidates.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "doc_number",
            "txn_date",
            "status",
            "amount",
            "debit_account",
            "credit_account",
            "basis",
            "needs",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(journals)
    with (OUT / "inventory_gl_purchases.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["kind", "id", "date", "month", "amount", "account", "memo"]
        )
        writer.writeheader()
        writer.writerows(inv_postings)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
