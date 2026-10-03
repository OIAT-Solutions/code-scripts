#!/usr/bin/env python3
"""Read-only: list QBO Transfers involving Undeposited Funds (company_a). GET queries only.

Writes qbo_transfers_uf_readonly.json (UF balance, every Transfer, to-UF / from-UF lists and the
Mar-Aug to-UF subset) into --out. This file is the delete-phase input of uf_reverse_and_allocate.

Example:
  python -m code_scripts.scripts.akponora_cutover.uf_list_transfers --out <dir>
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

import requests

from code_scripts.scripts.akponora_cutover._common import company_config, resolve_out

TOOL = "uf_list_transfers"


def money(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def qbo_query(base, realm, token, sql):
    url = f"{base}/v3/company/{realm}/query?query={quote(sql)}&minorversion=70"
    resp = requests.get(url, headers=headers(token), timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code} {resp.text[:800]}\n{sql}")
    return resp.json().get("QueryResponse", {})


def entities(qr, name):
    rows = qr.get(name) or []
    if not isinstance(rows, list):
        return [rows] if rows else []
    return rows


def paginate(base, realm, token, entity: str, extra: str = "") -> list:
    start = 1
    out = []
    while True:
        sql = f"select * from {entity}{extra} STARTPOSITION {start} MAXRESULTS 1000"
        batch = entities(qbo_query(base, realm, token, sql), entity)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        start += 1000
    return out


def flatten_transfer(t: dict) -> dict:
    frm = t.get("FromAccountRef") or {}
    to = t.get("ToAccountRef") or {}
    return {
        "Id": t.get("Id"),
        "SyncToken": t.get("SyncToken"),
        "TxnDate": t.get("TxnDate"),
        "Amount": str(money(t.get("Amount"))),
        "FromId": frm.get("value"),
        "FromName": frm.get("name"),
        "ToId": to.get("value"),
        "ToName": to.get("name"),
        "PrivateNote": t.get("PrivateNote") or "",
        "DocNumber": t.get("DocNumber") or "",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=None, help="output folder (default outputs/uf_list_transfers_<timestamp>/)")
    ap.add_argument("--window-from", default="2026-03-01", help="to-UF summary window start (default %(default)s)")
    ap.add_argument("--window-to", default="2026-08-31", help="to-UF summary window end (default %(default)s)")
    a = ap.parse_args(argv)
    OUT = resolve_out(a.out, TOOL)
    config = company_config("company_a")
    from code_scripts.company_config import get_qbo_api_base_url
    from code_scripts.token_manager import get_access_token

    token = get_access_token(config.company_key, config.realm_id)
    base = get_qbo_api_base_url(config.qbo_environment)
    realm = config.realm_id

    uf = entities(qbo_query(base, realm, token, "select * from Account where Id = '72'"), "Account")
    uf_bal = money((uf[0] or {}).get("CurrentBalance")) if uf else Decimal("0")

    transfers = paginate(base, realm, token, "Transfer")
    rows = [flatten_transfer(t) for t in transfers]

    uf_ids = {"72"}
    bank_like = set()
    to_uf = []
    from_uf = []
    for row in rows:
        to_uf_hit = row["ToId"] in uf_ids or "undeposited" in (row["ToName"] or "").lower() or "100900" in (row["ToName"] or "")
        from_uf_hit = row["FromId"] in uf_ids or "undeposited" in (row["FromName"] or "").lower() or "100900" in (row["FromName"] or "")
        if to_uf_hit:
            to_uf.append(row)
        if from_uf_hit:
            from_uf.append(row)

    def in_window(row, start=a.window_from, end=a.window_to):
        d = row.get("TxnDate") or ""
        return start <= d <= end

    mar_aug_to_uf = [r for r in to_uf if in_window(r)]
    mar_aug_to_uf_sum = sum((money(r["Amount"]) for r in mar_aug_to_uf), Decimal("0"))

    evidence = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "uf_balance": str(uf_bal),
        "transfer_count_all": len(rows),
        "to_uf_count": len(to_uf),
        "from_uf_count": len(from_uf),
        "mar_aug_to_uf_count": len(mar_aug_to_uf),
        "mar_aug_to_uf_sum": str(mar_aug_to_uf_sum),
        "target_count": 37,
        "target_sum": "1382097453.05",
        "to_uf": to_uf,
        "from_uf": from_uf,
        "all_transfers": rows,
    }
    path = OUT / "qbo_transfers_uf_readonly.json"
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({
        "wrote": str(path),
        "uf_balance": str(uf_bal),
        "all_transfers": len(rows),
        "to_uf": len(to_uf),
        "from_uf": len(from_uf),
        "mar_aug_to_uf": len(mar_aug_to_uf),
        "mar_aug_to_uf_sum": str(mar_aug_to_uf_sum),
        "sample_to_uf": [
            {"id": r["Id"], "date": r["TxnDate"], "amt": r["Amount"], "from": r["FromName"], "memo": r["PrivateNote"][:80]}
            for r in mar_aug_to_uf[:8]
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
