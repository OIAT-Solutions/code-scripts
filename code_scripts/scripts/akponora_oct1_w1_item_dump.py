#!/usr/bin/env python3
"""W1: read-only live QBO Item dump for Company A. No writes."""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("OIAT_COMPANIES_DIR", str(REPO / "code_scripts" / "companies"))

from code_scripts.company_config import get_qbo_api_base_url, load_company_config
from code_scripts.load_env import load_env_file
from code_scripts.token_manager import get_access_token

load_env_file()

OUT_DIR = REPO / "outputs" / "akponora_oct1_golive_2026-09-18"
MINOR = "70"
PAGE = 1000

WANTED_ACCOUNTS = {
    "77": "Inventory Asset",
    "86": "300150 - Historical Inventory Valuation Correction",
    "72": "Undeposited Funds",
    "1150040008": "120000 - Inventory",
    "1150040011": "120100 - Grocery",
    "1150040012": "120200 - Drinks",
    "1150040029": "120201 - Alcoholic",
    "1150040030": "120202 - Non-Alcoholic",
    "1150040013": "120300 - Non-food",
    "76": "200000 - Cost of sales",
    "78": "Cost of sales FIFO",
}


def money(value) -> Decimal:
    text = str(value or "").strip().replace(",", "")
    if not text or text == "-":
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def d2(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def qbo_query(base: str, realm: str, token: str, sql: str) -> dict:
    url = f"{base}/v3/company/{realm}/query?query={quote(sql)}&minorversion={MINOR}"
    resp = requests.get(url, headers=headers(token), timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"QBO query HTTP {resp.status_code}: {resp.text[:800]}\nSQL: {sql}")
    return resp.json().get("QueryResponse", {})


def entities(qr: dict, name: str) -> list:
    rows = qr.get(name) or []
    if not isinstance(rows, list):
        return [rows] if rows else []
    return rows


def paginate_items(base: str, realm: str, token: str, where: str = "") -> list[dict]:
    clause = f" {where.strip()}" if where.strip() else ""
    start = 1
    out: list[dict] = []
    select = f"select * from Item{clause}"
    while True:
        sql = f"{select} STARTPOSITION {start} MAXRESULTS {PAGE}"
        batch = entities(qbo_query(base, realm, token, sql), "Item")
        if not batch:
            break
        out.extend(batch)
        if len(batch) < PAGE:
            break
        start += PAGE
    return out


def flatten_item(item: dict) -> dict:
    meta = item.get("MetaData") or {}
    income = item.get("IncomeAccountRef") or {}
    expense = item.get("ExpenseAccountRef") or {}
    asset = item.get("AssetAccountRef") or {}
    qty = money(item.get("QtyOnHand"))
    avg = money(item.get("AvgCost"))
    purchase = money(item.get("PurchaseCost"))
    asset_value = qty * avg if item.get("Type") == "Inventory" else Decimal("0")
    return {
        "Id": item.get("Id") or "",
        "Name": item.get("Name") or "",
        "Sku": item.get("Sku") or "",
        "Type": item.get("Type") or "",
        "Active": item.get("Active"),
        "QtyOnHand": str(qty),
        "AvgCost": str(avg),
        "PurchaseCost": str(purchase),
        "UnitPrice": str(money(item.get("UnitPrice"))),
        "AssetValue": d2(asset_value),
        "TrackQtyOnHand": item.get("TrackQtyOnHand"),
        "InvStartDate": item.get("InvStartDate") or "",
        "Taxable": item.get("Taxable"),
        "SalesTaxIncluded": item.get("SalesTaxIncluded"),
        "IncomeAccountId": income.get("value") or "",
        "IncomeAccount": income.get("name") or "",
        "ExpenseAccountId": expense.get("value") or "",
        "ExpenseAccount": expense.get("name") or "",
        "AssetAccountId": asset.get("value") or "",
        "AssetAccount": asset.get("name") or "",
        "CreateTime": (meta.get("CreateTime") or "")[:19],
        "LastUpdatedTime": (meta.get("LastUpdatedTime") or "")[:19],
        "Description": item.get("Description") or "",
    }


def qty_bucket(qty: Decimal) -> str:
    if qty < 0:
        return "N"
    if qty == 0:
        return "Z"
    return "P"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_company_config("company_a")
    token = get_access_token(config.company_key, config.realm_id)
    base = get_qbo_api_base_url(config.qbo_environment)
    realm = config.realm_id
    as_of = datetime.now().isoformat(timespec="seconds")

    accounts = {}
    for acct_id, label in WANTED_ACCOUNTS.items():
        recs = entities(qbo_query(base, realm, token, f"select * from Account where Id = '{acct_id}'"), "Account")
        if recs:
            a = recs[0]
            accounts[label] = {
                "id": a.get("Id"),
                "name": a.get("Name"),
                "fully_qualified": a.get("FullyQualifiedName"),
                "active": a.get("Active"),
                "balance": d2(money(a.get("CurrentBalance"))),
                "type": a.get("AccountType"),
                "sub_type": a.get("AccountSubType"),
            }

    extra_names = [
        "400100 - Revenue - Grocery",
        "400201 - Alcoholic Drinks",
        "400202 - Non-Alcoholic Drinks",
        "400300 - Revenue - Non Food items",
        "200100 - Purchases - Groceries",
        "200201 - Purchases - Alcoholic Drinks",
        "200202 - Purchases - Non-Alcoholic Drinks",
        "200300 - Purchases - Non - food items",
        "200201 - Alcoholic Drinks",
        "200202 - Non-Alcoholic Drinks",
    ]
    for name in extra_names:
        safe = name.replace("'", "''")
        recs = entities(
            qbo_query(base, realm, token, f"select * from Account where Name = '{safe}'"),
            "Account",
        )
        if recs:
            a = recs[0]
            accounts[name] = {
                "id": a.get("Id"),
                "name": a.get("Name"),
                "fully_qualified": a.get("FullyQualifiedName"),
                "active": a.get("Active"),
                "balance": d2(money(a.get("CurrentBalance"))),
                "type": a.get("AccountType"),
                "sub_type": a.get("AccountSubType"),
            }

    active_items = paginate_items(base, realm, token)
    inactive_items = paginate_items(base, realm, token, "where Active = false")
    seen: set[str] = set()
    combined: list[dict] = []
    for item in active_items + inactive_items:
        item_id = str(item.get("Id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        combined.append(item)

    rows = [flatten_item(item) for item in combined]
    rows.sort(key=lambda r: (str(r["Type"]), str(r["Name"]).casefold(), str(r["Id"])))

    inv = [r for r in rows if r["Type"] == "Inventory"]
    buckets = {"Z0": 0, "Z+": 0, "P": 0, "N": 0, "inactive": 0}
    qty_sum = Decimal("0")
    value_sum = Decimal("0")
    created_sep = []
    for row in inv:
        qty = money(row["QtyOnHand"])
        value = money(row["AssetValue"])
        active = row["Active"] is True or str(row["Active"]).lower() == "true"
        if not active:
            buckets["inactive"] += 1
        bucket = qty_bucket(qty)
        residual_zero_qty = bucket == "Z" and value != 0
        if bucket == "Z" and not residual_zero_qty:
            buckets["Z0"] += 1
        elif residual_zero_qty:
            buckets["Z+"] += 1
        else:
            buckets[bucket] += 1
        if active:
            qty_sum += qty
            value_sum += value
        created = (row["CreateTime"] or "")[:10]
        if created >= "2026-09-01":
            created_sep.append(
                {
                    "id": row["Id"],
                    "name": row["Name"],
                    "sku": row["Sku"],
                    "created": created,
                    "active": active,
                    "qty": row["QtyOnHand"],
                    "value": row["AssetValue"],
                }
            )

    csv_path = OUT_DIR / "w1_qbo_items_live.csv"
    fieldnames = list(rows[0].keys()) if rows else ["Id", "Name", "Type"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    inv_csv = OUT_DIR / "w1_qbo_inventory_items_live.csv"
    inv_fields = fieldnames
    with inv_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=inv_fields)
        writer.writeheader()
        writer.writerows(inv)

    type_counts: dict[str, int] = {}
    active_counts: dict[str, int] = {}
    for row in rows:
        type_counts[row["Type"]] = type_counts.get(row["Type"], 0) + 1
        if row["Active"] is True or str(row["Active"]).lower() == "true":
            active_counts[row["Type"]] = active_counts.get(row["Type"], 0) + 1

    summary = {
        "as_of": as_of,
        "realm_id": realm,
        "read_only": True,
        "paths": {
            "all_items_csv": str(csv_path),
            "inventory_csv": str(inv_csv),
        },
        "item_type_counts": type_counts,
        "active_item_type_counts": active_counts,
        "inventory_qty_buckets": buckets,
        "inventory_active_qty_sum": str(qty_sum),
        "inventory_active_asset_value_sum_qty_times_avgcost": d2(value_sum),
        "inventory_created_since_2026_09_01": created_sep,
        "accounts": accounts,
        "notes": [
            "AssetValue is QtyOnHand * AvgCost from the Item query (QBO does not always return AssetValue directly).",
            "Inactive items are included. Bucket inactive is counted separately from Z0/Z+/P/N which include inactive.",
            "Do not use this dump to patch QtyOnHand or inactivate items.",
        ],
    }
    summary_path = OUT_DIR / "w1_qbo_item_dump_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote_csv": str(csv_path),
        "wrote_inventory_csv": str(inv_csv),
        "wrote_summary": str(summary_path),
        "type_counts": type_counts,
        "active_counts": active_counts,
        "inventory_buckets": buckets,
        "inventory_active_qty_sum": str(qty_sum),
        "inventory_active_value_sum": d2(value_sum),
        "created_since_sep1": len(created_sep),
        "account_keys": list(accounts),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
