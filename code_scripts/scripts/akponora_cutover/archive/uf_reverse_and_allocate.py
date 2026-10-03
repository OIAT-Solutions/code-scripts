#!/usr/bin/env python3
"""Reverse Bank→UF monthly-sales transfers, then deposit remaining UF using the till sheet.

Company A production. Explicit chat yes 26 Sep 2026.

Phases:
  probe   — balances, the 37 transfers, undeposited sales receipts, existing deposits (GET only)
  delete  — delete those 37 Bank→UF transfers only (not the other 9 transfers, not journals)
  deposit — Bank Deposits linking Jan 1–24 Sep sales receipts, assigned from the till sheet
  tail    — deposit receipts after the sheet's last filled day, scaled from that day's mix
  all     — probe, delete, deposit

ALREADY EXECUTED on 26 Sep 2026 (outputs/akponora_uf_allocation_2026-09-26/). Do NOT re-run
delete/deposit/tail. The default phase is now ``probe`` (read-only); every other phase
writes to QBO and refuses to run without ``--execute`` (and a fresh chat yes).

Inputs (--inputs-dir, default outputs/akponora_uf_allocation_2026-09-26):
  qbo_transfers_uf_readonly.json (uf_list_transfers) and proposed_deposits.csv (uf_allocation_draft).
Evidence JSON/report are written to --out (default outputs/uf_reverse_and_allocate_<timestamp>/).

Example (read-only):
  python -m code_scripts.scripts.akponora_cutover.uf_reverse_and_allocate --phase probe
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import quote

from code_scripts.scripts.akponora_cutover._common import REPO_ROOT, company_config, resolve_out

TOOL = "uf_reverse_and_allocate"
INPUTS_DIR = REPO_ROOT / "outputs" / "akponora_uf_allocation_2026-09-26"
# Set from the command line in main().
OUT = INPUTS_DIR
EVIDENCE = INPUTS_DIR / "qbo_transfers_uf_readonly.json"
DEPOSITS_CSV = INPUTS_DIR / "proposed_deposits.csv"
EXECUTE = False
MINORVERSION = "70"
SR_FIELDS = "Id, DocNumber, TxnDate, TotalAmt, PrivateNote, SyncToken"
UF_ID = "72"
COMPANY_KEY = "company_a"
SR_FROM = "2026-01-01"
SR_TO = "2026-09-24"  # fallback; deposit/tail use live last SalesReceipt.TxnDate
TARGET_COUNT = 37
TARGET_SUM = Decimal("1382097453.05")
DOC_PREFIX = "UF"

BANKS = {
    "100100": {"qbo_id": "29", "name": "100100 - Petty Cash"},
    "100301": {"qbo_id": "1150040044", "name": "100301 - Zenith Bank 1225575438"},
    "100207": {"qbo_id": "1150040041", "name": "100207 - MONIEPOINT 4000850527"},
    "100205": {"qbo_id": "1150040005", "name": "100205 - MONIEPOINT 6397730972"},
    "100206": {"qbo_id": "1150040040", "name": "100206 - MONIEPOINT 4000850479"},
    "100201": {"qbo_id": "1150040001", "name": "100201 - MONIEPOINT 4000700275"},
    "100202": {"qbo_id": "1150040002", "name": "100202 - MONIEPOINT 4686987227"},
}
WATCH_ACCOUNT_IDS = [UF_ID, "31", "29", "1150040044", "1150040041", "1150040005", "1150040040", "1150040001", "1150040002"]
CARD_BANKS = ["100301", "100207", "100206"]
TRANSFER_BANKS = ["100205", "100202"]
CASH_BANKS = ["100100"]
ALLOC_BANKS = ["100100", "100301", "100207", "100206", "100205", "100202"]


def _make_qbo_request(method, url, token_mgr, **kwargs):
    """Pipeline request helper, refusing anything but GET unless --execute was given."""
    if method.upper() != "GET" and not EXECUTE:
        raise RuntimeError(f"refusing {method} without --execute")
    from code_scripts.qbo_upload import _make_qbo_request as request

    return request(method, url, token_mgr, **kwargs)


def money(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


def d2(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def query_url(base: str, realm: str, sql: str) -> str:
    return f"{base}/v3/company/{realm}/query?query={quote(sql)}&minorversion={MINORVERSION}"


def entities(qr: dict, name: str) -> list:
    rows = (qr or {}).get(name) or []
    if not isinstance(rows, list):
        return [rows] if rows else []
    return rows


def qbo_query(token_mgr: TokenManager, base: str, realm: str, sql: str) -> dict:
    resp = _make_qbo_request("GET", query_url(base, realm, sql), token_mgr)
    if resp.status_code >= 400:
        raise RuntimeError(f"QBO query {resp.status_code}: {resp.text[:800]}\n{sql}")
    return resp.json().get("QueryResponse", {})


def paginate(token_mgr: TokenManager, base: str, realm: str, entity: str, extra: str = "", fields: str = "*") -> list:
    start = 1
    out: list = []
    while True:
        sql = f"select {fields} from {entity}{extra} STARTPOSITION {start} MAXRESULTS 1000"
        print(f"[INFO] query {entity} start={start}", flush=True)
        batch = entities(qbo_query(token_mgr, base, realm, sql), entity)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1000:
            break
        start += 1000
    return out


def get_entity(token_mgr: TokenManager, base: str, realm: str, entity: str, entity_id: str) -> tuple[int, dict]:
    url = f"{base}/v3/company/{realm}/{entity.lower()}/{entity_id}?minorversion={MINORVERSION}"
    resp = _make_qbo_request("GET", url, token_mgr)
    payload = {}
    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": (resp.text or "")[:500]}
    return resp.status_code, payload


def snapshot_accounts(token_mgr: TokenManager, base: str, realm: str) -> dict:
    out = {}
    for acc_id in WATCH_ACCOUNT_IDS:
        rows = entities(qbo_query(token_mgr, base, realm, f"select * from Account where Id = '{acc_id}'"), "Account")
        if not rows:
            continue
        acc = rows[0]
        out[acc.get("Id")] = {
            "Id": acc.get("Id"),
            "Name": acc.get("FullyQualifiedName") or acc.get("Name"),
            "CurrentBalance": str(money(acc.get("CurrentBalance"))),
            "AccountType": acc.get("AccountType"),
        }
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


def load_targets() -> list[dict]:
    if not EVIDENCE.exists():
        raise SystemExit(f"Missing {EVIDENCE}; run uf_list_transfers first")
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    rows = data.get("to_uf") or []
    if len(rows) != TARGET_COUNT:
        raise SystemExit(f"Expected {TARGET_COUNT} to-UF transfers, evidence has {len(rows)}")
    total = sum((money(r["Amount"]) for r in rows), Decimal("0"))
    if total != TARGET_SUM:
        raise SystemExit(f"Expected sum {TARGET_SUM}, evidence has {total}")
    return rows


def last_sales_receipt_date(token_mgr, base, realm) -> str:
    rows = entities(
        qbo_query(
            token_mgr,
            base,
            realm,
            "select Id, TxnDate from SalesReceipt ORDERBY TxnDate DESC MAXRESULTS 1",
        ),
        "SalesReceipt",
    )
    day = (rows[0].get("TxnDate") if rows else "") or ""
    if not day:
        raise RuntimeError("No Sales Receipts in QBO; cannot set allocation end date")
    return day


def linked_sales_receipt_ids(token_mgr, base, realm) -> set[str]:
    ids: set[str] = set()
    for dep in paginate(token_mgr, base, realm, "Deposit"):
        for line in dep.get("Line") or []:
            for link in line.get("LinkedTxn") or []:
                if (link.get("TxnType") or "") == "SalesReceipt" and link.get("TxnId"):
                    ids.add(str(link.get("TxnId")))
    return ids


def scaled_targets(template: dict[str, Decimal], total: Decimal) -> dict[str, Decimal]:
    base_total = sum(template.values(), Decimal("0"))
    if base_total <= 0 or total <= 0:
        return {code: Decimal("0") for code in ALLOC_BANKS}
    out = {
        code: (total * money(template.get(code, Decimal("0"))) / base_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        for code in ALLOC_BANKS
    }
    out["100202"] += total - sum(out.values(), Decimal("0"))
    return out


def load_sheet_targets() -> dict[str, dict[str, Decimal]]:
    daily: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    with DEPOSITS_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            day = (row.get("Date") or "").strip()
            bank = (row.get("Bank code") or "").strip()
            amt = money(row.get("Amount"))
            if not day or amt == 0 or bank not in BANKS:
                continue
            daily[day][bank] += amt
    return {day: dict(vals) for day, vals in daily.items()}


def sr_undeposited(sr: dict, deposited_ids: set[str]) -> bool:
    sid = str(sr.get("Id") or "")
    if not sid or sid in deposited_ids:
        return False
    deposit_to = (sr.get("DepositToAccountRef") or {}).get("value")
    if deposit_to and deposit_to != UF_ID:
        return False
    for link in sr.get("LinkedTxn") or []:
        if (link.get("TxnType") or "") == "Deposit":
            return False
    return True


def tender_of(sr: dict) -> str:
    pm = ((sr.get("PaymentMethodRef") or {}).get("name") or "").strip()
    memo = (sr.get("PrivateNote") or "").strip()
    customer_memo = ((sr.get("CustomerMemo") or {}) if isinstance(sr.get("CustomerMemo"), dict) else {}).get("value") or ""
    text = (pm or memo or customer_memo or "").strip()
    lower = text.lower()
    if "/" in lower:
        return "Mixed"
    if lower == "cash":
        return "Cash"
    if lower == "card":
        return "Card"
    if lower == "transfer":
        return "Transfer"
    if "cash" in lower and "card" not in lower and "transfer" not in lower:
        return "Cash"
    if "card" in lower and "cash" not in lower and "transfer" not in lower:
        return "Card"
    if "transfer" in lower and "cash" not in lower and "card" not in lower:
        return "Transfer"
    return "Mixed"


def raw_tender_text(sr: dict) -> str:
    pm = ((sr.get("PaymentMethodRef") or {}).get("name") or "").strip()
    memo = (sr.get("PrivateNote") or "").strip()
    customer_memo = ((sr.get("CustomerMemo") or {}) if isinstance(sr.get("CustomerMemo"), dict) else {}).get("value") or ""
    return (pm or memo or customer_memo or "").strip()


def candidate_banks(tender: str, remaining: dict[str, Decimal], raw: str = "") -> list[str]:
    if tender == "Cash":
        pool = list(CASH_BANKS)
    elif tender == "Card":
        pool = list(CARD_BANKS)
    elif tender == "Transfer":
        pool = list(TRANSFER_BANKS)
    else:
        lower = raw.lower()
        pool = []
        if "cash" in lower:
            pool.extend(CASH_BANKS)
        if "card" in lower:
            pool.extend(CARD_BANKS)
        if "transfer" in lower:
            pool.extend(TRANSFER_BANKS)
        if not pool:
            pool = list(ALLOC_BANKS)
    live = [b for b in pool if remaining.get(b, Decimal("0")) > 0]
    return live or pool or list(ALLOC_BANKS)


def assign_srs(srs: list[dict], targets: dict[str, Decimal]) -> dict[str, list[dict]]:
    remaining = {code: money(targets.get(code, Decimal("0"))) for code in ALLOC_BANKS}
    assigned: dict[str, list[dict]] = defaultdict(list)
    ordered = sorted(srs, key=lambda row: (tender_of(row) == "Mixed", -money(row.get("TotalAmt"))))
    for sr in ordered:
        amt = money(sr.get("TotalAmt"))
        if amt <= 0:
            continue
        tender = tender_of(sr)
        cands = candidate_banks(tender, remaining, raw_tender_text(sr))
        fits = [b for b in cands if remaining.get(b, Decimal("0")) >= amt]
        if fits:
            bank = min(fits, key=lambda b: remaining[b] - amt)
        else:
            bank = max(cands, key=lambda b: remaining.get(b, Decimal("0")))
        assigned[bank].append(sr)
        remaining[bank] = remaining.get(bank, Decimal("0")) - amt
    return assigned


def deposit_docnumber(day: str, bank: str) -> str:
    yymmdd = day[2:4] + day[5:7] + day[8:10]
    return f"{DOC_PREFIX}{yymmdd}{bank}"  # e.g. UF260101100100 (14 chars)


def trueup_docnumber(src: str, dst: str) -> str:
    return f"UFTU{src[-3:]}{dst[-3:]}"  # e.g. UFTU202205 (10 chars)


def deposited_sr_ids(deposits: list[dict]) -> set[str]:
    ids: set[str] = set()
    for dep in deposits:
        for line in dep.get("Line") or []:
            for link in line.get("LinkedTxn") or []:
                if (link.get("TxnType") or "") in {"SalesReceipt", "Payment"}:
                    ids.add(str(link.get("TxnId")))
    return ids


def connect():
    config = company_config(COMPANY_KEY)  # sets OIAT_COMPANIES_DIR before code_scripts.paths is imported
    from code_scripts.company_config import get_qbo_api_base_url
    from code_scripts.qbo_upload import TokenManager
    from code_scripts.token_manager import verify_realm_match

    verify_realm_match(COMPANY_KEY, config.realm_id)
    token_mgr = TokenManager(config.company_key, config.realm_id)
    base = get_qbo_api_base_url(config.qbo_environment)
    return config, token_mgr, base, config.realm_id


def phase_probe(token_mgr, base, realm) -> dict:
    print("[INFO] Probe: accounts, transfers, sales receipts, deposits", flush=True)
    accounts = snapshot_accounts(token_mgr, base, realm)
    transfers = [flatten_transfer(t) for t in paginate(token_mgr, base, realm, "Transfer")]
    to_uf = [t for t in transfers if t["ToId"] == UF_ID]
    other = [t for t in transfers if t["ToId"] != UF_ID]
    deposits = paginate(token_mgr, base, realm, "Deposit")
    srs = paginate(
        token_mgr,
        base,
        realm,
        "SalesReceipt",
        extra=f" where TxnDate >= '{SR_FROM}' and TxnDate <= '{SR_TO}'",
        fields=SR_FIELDS,
    )
    already = deposited_sr_ids(deposits)
    undeposited = [sr for sr in srs if sr_undeposited(sr, already)]
    by_tender = defaultdict(lambda: [0, Decimal("0")])
    for sr in undeposited:
        t = tender_of(sr)
        by_tender[t][0] += 1
        by_tender[t][1] += money(sr.get("TotalAmt"))
    probe = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "accounts": accounts,
        "uf_balance": (accounts.get(UF_ID) or {}).get("CurrentBalance"),
        "transfer_count": len(transfers),
        "to_uf_count": len(to_uf),
        "to_uf_sum": str(sum((money(t["Amount"]) for t in to_uf), Decimal("0"))),
        "other_transfers": other,
        "deposit_count": len(deposits),
        "sr_count_window": len(srs),
        "sr_undeposited_count": len(undeposited),
        "sr_undeposited_sum": str(sum((money(sr.get("TotalAmt")) for sr in undeposited), Decimal("0"))),
        "sr_by_tender": {k: {"count": n, "sum": str(s)} for k, (n, s) in sorted(by_tender.items())},
        "existing_deposit_docnumbers": [d.get("DocNumber") for d in deposits if d.get("DocNumber")],
    }
    path = OUT / "qbo_uf_probe.json"
    path.write_text(json.dumps(probe, indent=2), encoding="utf-8")
    print(json.dumps({k: probe[k] for k in probe if k != "accounts" and k != "other_transfers" and k != "existing_deposit_docnumbers"}, indent=2))
    print(f"[INFO] wrote {path}")
    return probe


def phase_delete(token_mgr, base, realm) -> dict:
    targets = load_targets()
    print(f"[INFO] Deleting {len(targets)} Bank→UF transfers totaling {TARGET_SUM}", flush=True)
    results = []
    ok = 0
    fail = 0
    skipped = 0
    deleted_sum = Decimal("0")
    for row in targets:
        tid = str(row["Id"])
        status, payload = get_entity(token_mgr, base, realm, "transfer", tid)
        if status == 404 or payload.get("Fault"):
            skipped += 1
            results.append({"Id": tid, "status": "missing", "http": status})
            print(f"  [SKIP] Transfer {tid} already gone")
            continue
        live = payload.get("Transfer") or {}
        live_row = flatten_transfer(live) if live else {}
        if live_row.get("ToId") != UF_ID:
            fail += 1
            results.append({"Id": tid, "status": "refused_not_to_uf", "live": live_row})
            print(f"  [FAIL] Transfer {tid} is not To UF; left in place")
            continue
        if money(live_row.get("Amount")) != money(row["Amount"]):
            fail += 1
            results.append({"Id": tid, "status": "refused_amount_mismatch", "expected": row["Amount"], "live": live_row})
            print(f"  [FAIL] Transfer {tid} amount changed; left in place")
            continue
        url = f"{base}/v3/company/{realm}/transfer?operation=delete&minorversion={MINORVERSION}"
        body = {"Id": tid, "SyncToken": str(live.get("SyncToken", row.get("SyncToken") or "0"))}
        resp = _make_qbo_request("POST", url, token_mgr, json=body)
        if resp.status_code in (200, 201):
            ok += 1
            deleted_sum += money(row["Amount"])
            results.append({"Id": tid, "status": "deleted", "amount": row["Amount"], "date": row["TxnDate"], "from": row["FromName"], "memo": row["PrivateNote"]})
            print(f"  [OK] {tid} {row['TxnDate']} {row['Amount']} {row['FromName'].split(':')[-1]}")
        else:
            fail += 1
            results.append({"Id": tid, "status": "failed", "http": resp.status_code, "body": resp.text[:500]})
            print(f"  [FAIL] {tid}: HTTP {resp.status_code} {resp.text[:200]}")
        time.sleep(0.05)
    accounts = snapshot_accounts(token_mgr, base, realm)
    evidence = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "deleted": ok,
        "failed": fail,
        "skipped": skipped,
        "deleted_sum": str(deleted_sum),
        "target_sum": str(TARGET_SUM),
        "uf_after": (accounts.get(UF_ID) or {}).get("CurrentBalance"),
        "accounts_after": accounts,
        "results": results,
    }
    path = OUT / "qbo_transfers_deleted.json"
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("deleted", "failed", "skipped", "deleted_sum", "uf_after")}, indent=2))
    print(f"[INFO] wrote {path}")
    if fail:
        raise SystemExit(f"Delete phase had {fail} failure(s); not continuing to deposits")
    return evidence


def post_deposit_mv65(token_mgr, base, realm, payload: dict) -> tuple[bool, str, dict]:
    url = f"{base}/v3/company/{realm}/deposit?minorversion=65"
    resp = _make_qbo_request("POST", url, token_mgr, json=payload)
    try:
        body = resp.json()
    except Exception:
        body = {"raw": (resp.text or "")[:500]}
    if resp.status_code in (200, 201):
        created = body.get("Deposit") or {}
        return True, str(created.get("Id") or ""), created
    detail = body.get("Fault", {}).get("Error", []) if isinstance(body, dict) else []
    msg = "; ".join((e.get("Detail") or e.get("Message") or str(e)) for e in detail) if detail else str(body)[:400]
    return False, "", {"http": resp.status_code, "error": msg}


def deposit_payload(day: str, bank: str, group: list[dict], doc: str) -> dict:
    return {
        "DepositToAccountRef": {"value": BANKS[bank]["qbo_id"]},
        "TxnDate": day,
        "DocNumber": doc,
        "PrivateNote": f"UF alloc {day} {bank} Nora Mini Mart till sheet",
        "Line": [
            {
                "Amount": float(d2(money(sr.get("TotalAmt")))),
                "LinkedTxn": [{"TxnId": str(sr.get("Id")), "TxnType": "SalesReceipt", "TxnLineId": "0"}],
            }
            for sr in group
        ],
    }


def post_transfer(token_mgr, base, realm, payload: dict) -> tuple[bool, str, dict]:
    url = f"{base}/v3/company/{realm}/transfer?minorversion={MINORVERSION}"
    resp = _make_qbo_request("POST", url, token_mgr, json=payload)
    try:
        body = resp.json()
    except Exception:
        body = {"raw": (resp.text or "")[:500]}
    if resp.status_code in (200, 201):
        created = body.get("Transfer") or {}
        return True, str(created.get("Id") or ""), created
    detail = body.get("Fault", {}).get("Error", []) if isinstance(body, dict) else []
    msg = "; ".join((e.get("Detail") or e.get("Message") or str(e)) for e in detail) if detail else str(body)[:400]
    return False, "", {"http": resp.status_code, "error": msg}


def phase_deposit(token_mgr, base, realm) -> dict:
    sheet = load_sheet_targets()
    sheet_total = sum((sum(banks.values(), Decimal("0")) for banks in sheet.values()), Decimal("0"))
    print(f"[INFO] Sheet targets {len(sheet)} days totaling {d2(sheet_total)}", flush=True)

    srs = paginate(
        token_mgr,
        base,
        realm,
        "SalesReceipt",
        extra=f" where TxnDate >= '{SR_FROM}' and TxnDate <= '{SR_TO}'",
        fields=SR_FIELDS,
    )
    by_day: dict[str, list[dict]] = defaultdict(list)
    for sr in srs:
        by_day[sr.get("TxnDate") or ""].append(sr)
    print(
        f"[INFO] Sales receipts in window: {len(srs)} totaling {d2(sum((money(sr.get('TotalAmt')) for sr in srs), Decimal('0')))}",
        flush=True,
    )
    print("[INFO] Depositing receipts via Bank Deposit LinkedTxn (minorversion 65)", flush=True)
    existing_docs = {str(d.get("DocNumber") or "") for d in paginate(token_mgr, base, realm, "Deposit") if d.get("DocNumber")}
    qbo_id_to_bank = {meta["qbo_id"]: code for code, meta in BANKS.items()}

    posted = []
    skipped = []
    failed = []
    assigned_total = Decimal("0")
    posted_by_bank = defaultdict(lambda: Decimal("0"))
    srs_deposited = 0

    days = sorted(set(sheet) | set(by_day))
    for day in days:
        if not day or day > SR_TO:
            continue
        day_srs = by_day.get(day) or []
        targets = sheet.get(day) or {}
        if not day_srs:
            if any(money(v) for v in targets.values()):
                skipped.append({"date": day, "reason": "sheet_row_no_srs", "sheet": {k: d2(v) for k, v in targets.items()}})
            continue
        assigned = assign_srs(day_srs, targets)
        for bank, group in sorted(assigned.items()):
            if not group:
                continue
            amount = sum((money(sr.get("TotalAmt")) for sr in group), Decimal("0"))
            doc = deposit_docnumber(day, bank)
            if doc in existing_docs:
                skipped.append({"date": day, "bank": bank, "reason": "docnumber_exists", "DocNumber": doc, "amount": d2(amount)})
                posted_by_bank[bank] += amount
                assigned_total += amount
                srs_deposited += len(group)
                continue

            def record_success(dep_id, used_group, used_amount):
                nonlocal assigned_total, srs_deposited
                rec = {
                    "date": day,
                    "bank": bank,
                    "qbo_id": BANKS[bank]["qbo_id"],
                    "DocNumber": doc,
                    "amount": d2(used_amount),
                    "DepositId": dep_id,
                    "sr_ids": [str(sr.get("Id")) for sr in used_group],
                    "sr_docs": [sr.get("DocNumber") for sr in used_group],
                    "sheet_target": d2(targets.get(bank, Decimal("0"))),
                }
                posted.append(rec)
                posted_by_bank[bank] += used_amount
                assigned_total += used_amount
                srs_deposited += len(used_group)
                existing_docs.add(doc)
                print(f"  [OK] {doc} {d2(used_amount)} → {bank} ({len(used_group)} SR) Id={dep_id}", flush=True)

            ok, dep_id, body = post_deposit_mv65(token_mgr, base, realm, deposit_payload(day, bank, group, doc))
            if ok:
                record_success(dep_id, group, amount)
                continue

            still = []
            for sr in group:
                status, payload = get_entity(token_mgr, base, realm, "salesreceipt", str(sr.get("Id")))
                live = (payload or {}).get("SalesReceipt") or {}
                current = (live.get("DepositToAccountRef") or {}).get("value")
                if current == UF_ID:
                    still.append(sr)
                else:
                    actual_bank = qbo_id_to_bank.get(current, bank)
                    posted_by_bank[actual_bank] += money(sr.get("TotalAmt"))
                    assigned_total += money(sr.get("TotalAmt"))
                    srs_deposited += 1
                    skipped.append({
                        "date": day,
                        "bank": actual_bank,
                        "reason": "already_on_bank",
                        "sr_id": str(sr.get("Id")),
                        "sr_doc": sr.get("DocNumber"),
                        "amount": d2(money(sr.get("TotalAmt"))),
                        "deposit_account": current,
                    })
            if still:
                still_amt = sum((money(sr.get("TotalAmt")) for sr in still), Decimal("0"))
                ok2, dep_id2, body2 = post_deposit_mv65(token_mgr, base, realm, deposit_payload(day, bank, still, doc))
                if ok2:
                    record_success(dep_id2, still, still_amt)
                else:
                    failed.append({"date": day, "bank": bank, "DocNumber": doc, "error": body2, "sr_ids": [str(sr.get("Id")) for sr in still]})
                    print(f"  [FAIL] {doc}: {body2}", flush=True)
            elif not still and group:
                print(f"  [SKIP] {doc} all {len(group)} SRs already on banks ({body.get('error','')[:80]})", flush=True)

    trueups = []
    if assigned_total > 0 and sheet_total > 0:
        target_by_bank = {
            bank: (assigned_total * sum((day.get(bank, Decimal("0")) for day in sheet.values()), Decimal("0")) / sheet_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for bank in ALLOC_BANKS
        }
        residue = assigned_total - sum(target_by_bank.values(), Decimal("0"))
        target_by_bank["100202"] += residue
        surplus = {b: posted_by_bank[b] - target_by_bank[b] for b in ALLOC_BANKS}
        sources = [b for b in ALLOC_BANKS if surplus[b] > Decimal("0.01")]
        sinks = [b for b in ALLOC_BANKS if surplus[b] < Decimal("-0.01")]
        print("[INFO] True-up surplus", {k: d2(v) for k, v in surplus.items() if v != 0}, flush=True)
        for src in sources:
            for dst in sinks:
                if surplus[src] <= Decimal("0.01"):
                    break
                need = -surplus[dst]
                if need <= Decimal("0.01"):
                    continue
                move = min(surplus[src], need).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if move <= 0:
                    continue
                doc = trueup_docnumber(src, dst)
                payload = {
                    "FromAccountRef": {"value": BANKS[src]["qbo_id"]},
                    "ToAccountRef": {"value": BANKS[dst]["qbo_id"]},
                    "Amount": float(d2(move)),
                    "TxnDate": SR_TO,
                    "DocNumber": doc,
                    "PrivateNote": f"UF allocation true-up {src} → {dst} to Nora Mini Mart till sheet mix",
                }
                ok, xid, body = post_transfer(token_mgr, base, realm, payload)
                rec = {"from": src, "to": dst, "amount": d2(move), "DocNumber": doc}
                if ok:
                    rec["TransferId"] = xid
                    surplus[src] -= move
                    surplus[dst] += move
                    posted_by_bank[src] -= move
                    posted_by_bank[dst] += move
                    print(f"  [OK] true-up {doc} {d2(move)} {src} → {dst} Id={xid}", flush=True)
                else:
                    rec["error"] = body
                    print(f"  [FAIL] true-up {doc}: {body}", flush=True)
                trueups.append(rec)

    accounts = snapshot_accounts(token_mgr, base, realm)
    evidence = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "method": "Bank Deposit LinkedTxn minorversion=65 TxnLineId=0, destinations from Nora Mini Mart till sheet",
        "sheet_total": d2(sheet_total),
        "sr_in_window": len(srs),
        "deposits_posted": len(posted),
        "srs_deposited": srs_deposited,
        "deposits_failed": len(failed),
        "skipped": skipped,
        "deposited_sum": d2(assigned_total),
        "by_bank_after_trueup": {k: d2(posted_by_bank[k]) for k in ALLOC_BANKS + ["100201"]},
        "uf_after": (accounts.get(UF_ID) or {}).get("CurrentBalance"),
        "accounts_after": accounts,
        "posted": posted,
        "failed": failed,
        "trueups": trueups,
    }
    path = OUT / "qbo_deposits_posted.json"
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: evidence[k] for k in ("deposits_posted", "srs_deposited", "deposits_failed", "deposited_sum", "by_bank_after_trueup", "uf_after")},
            indent=2,
        ),
        flush=True,
    )
    print(f"[INFO] wrote {path}", flush=True)
    return evidence


def phase_tail(token_mgr, base, realm) -> dict:
    last_date = last_sales_receipt_date(token_mgr, base, realm)
    sheet = load_sheet_targets()
    filled = [day for day, banks in sheet.items() if sum(banks.values(), Decimal("0")) > 0]
    last_filled = max(filled) if filled else last_date
    print(f"[INFO] QBO last SalesReceipt date {last_date}; sheet last filled {last_filled}", flush=True)

    srs = paginate(
        token_mgr,
        base,
        realm,
        "SalesReceipt",
        extra=f" where TxnDate >= '{SR_FROM}' and TxnDate <= '{last_date}'",
        fields=SR_FIELDS,
    )
    already = linked_sales_receipt_ids(token_mgr, base, realm)
    unlinked = [sr for sr in srs if str(sr.get("Id")) not in already and money(sr.get("TotalAmt")) > 0]
    print(
        f"[INFO] SRs through {last_date}: {len(srs)}; already deposited {len(already)}; unlinked nonzero {len(unlinked)} totaling {d2(sum((money(sr.get('TotalAmt')) for sr in unlinked), Decimal('0')))}",
        flush=True,
    )
    by_day: dict[str, list[dict]] = defaultdict(list)
    for sr in unlinked:
        by_day[sr.get("TxnDate") or ""].append(sr)

    posted = []
    failed = []
    assigned_total = Decimal("0")
    posted_by_bank = defaultdict(lambda: Decimal("0"))
    srs_deposited = 0
    existing_docs = {str(d.get("DocNumber") or "") for d in paginate(token_mgr, base, realm, "Deposit") if d.get("DocNumber")}

    for day in sorted(by_day):
        day_srs = by_day[day]
        day_total = sum((money(sr.get("TotalAmt")) for sr in day_srs), Decimal("0"))
        if sheet.get(day) and sum(sheet[day].values(), Decimal("0")) > 0:
            targets = sheet[day]
            source = "sheet"
        else:
            targets = scaled_targets(sheet.get(last_filled) or {}, day_total)
            source = f"scaled_from_{last_filled}"
        assigned = assign_srs(day_srs, targets)
        for bank, group in sorted(assigned.items()):
            if not group:
                continue
            amount = sum((money(sr.get("TotalAmt")) for sr in group), Decimal("0"))
            doc = deposit_docnumber(day, bank)
            if doc in existing_docs:
                print(f"  [SKIP] {doc} exists", flush=True)
                continue
            ok, dep_id, body = post_deposit_mv65(token_mgr, base, realm, deposit_payload(day, bank, group, doc))
            rec = {
                "date": day,
                "bank": bank,
                "amount": d2(amount),
                "DocNumber": doc,
                "source": source,
                "sr_ids": [str(sr.get("Id")) for sr in group],
                "sr_docs": [sr.get("DocNumber") for sr in group],
            }
            if ok:
                rec["DepositId"] = dep_id
                posted.append(rec)
                posted_by_bank[bank] += amount
                assigned_total += amount
                srs_deposited += len(group)
                existing_docs.add(doc)
                print(f"  [OK] {doc} {d2(amount)} → {bank} ({len(group)} SR) Id={dep_id}", flush=True)
            else:
                rec["error"] = body
                failed.append(rec)
                print(f"  [FAIL] {doc}: {body}", flush=True)

    accounts = snapshot_accounts(token_mgr, base, realm)
    evidence = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "qbo_last_sales_receipt_date": last_date,
        "sheet_last_filled": last_filled,
        "srs_through_last_date": len(srs),
        "already_linked": len(already),
        "unlinked_nonzero": len(unlinked),
        "deposits_posted": len(posted),
        "deposits_failed": len(failed),
        "srs_deposited": srs_deposited,
        "deposited_sum": d2(assigned_total),
        "by_bank": {k: d2(posted_by_bank[k]) for k in ALLOC_BANKS},
        "uf_after": (accounts.get(UF_ID) or {}).get("CurrentBalance"),
        "accounts_after": accounts,
        "posted": posted,
        "failed": failed,
    }
    path = OUT / "qbo_deposits_tail.json"
    path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({k: evidence[k] for k in ("qbo_last_sales_receipt_date", "sheet_last_filled", "unlinked_nonzero", "deposits_posted", "deposited_sum", "uf_after")}, indent=2), flush=True)
    print(f"[INFO] wrote {path}", flush=True)
    return evidence


def write_report(delete_ev: dict | None, deposit_ev: dict | None) -> None:
    lines = [
        "# Undeposited Funds reverse + allocate — 26 Sep 2026",
        "",
        "Company A production. Reversed the 37 Bank→UF monthly-sales transfers, then deposited remaining till receipts using the Nora Mini Mart daily sheet.",
        "",
    ]
    if delete_ev:
        lines += [
            "## Transfers reversed",
            "",
            f"- Deleted **{delete_ev.get('deleted')}** transfers totaling ₦{delete_ev.get('deleted_sum')}",
            f"- Failed: {delete_ev.get('failed')}  Skipped: {delete_ev.get('skipped')}",
            f"- UF after reverse: ₦{delete_ev.get('uf_after')}",
            "",
        ]
    if deposit_ev:
        lines += [
            "## Deposits",
            "",
            f"- Posted **{deposit_ev.get('deposits_posted')}** Bank Deposits covering {deposit_ev.get('srs_deposited')} receipts totaling ₦{deposit_ev.get('deposited_sum')}",
            f"- Failed: {deposit_ev.get('deposits_failed')}",
            f"- UF after deposits: ₦{deposit_ev.get('uf_after')}",
            "",
            "By bank (after true-up):",
            "",
        ]
        for bank, amt in (deposit_ev.get("by_bank_after_trueup") or {}).items():
            lines.append(f"- `{bank}` ₦{amt}")
        lines.append("")
    path = OUT / "UF_REVERSE_AND_ALLOCATE_REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[INFO] wrote {path}")


def main(argv=None) -> int:
    global OUT, EVIDENCE, DEPOSITS_CSV, EXECUTE, TARGET_COUNT, TARGET_SUM
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=["probe", "delete", "deposit", "tail", "all"], default="probe")
    parser.add_argument("--execute", action="store_true",
                        help="required for delete/deposit/tail/all (QBO writes). Already executed 26 Sep 2026.")
    parser.add_argument("--inputs-dir", type=Path, default=INPUTS_DIR,
                        help="folder with qbo_transfers_uf_readonly.json and proposed_deposits.csv (default %(default)s)")
    parser.add_argument("--target-count", type=int, default=TARGET_COUNT, help="expected Bank->UF transfers (delete guard)")
    parser.add_argument("--target-sum", type=Decimal, default=TARGET_SUM, help="expected their sum (delete guard)")
    parser.add_argument("--out", type=Path, default=None, help="evidence folder (default outputs/uf_reverse_and_allocate_<timestamp>/)")
    args = parser.parse_args(argv)
    if args.phase != "probe" and not args.execute:
        parser.error(f"--phase {args.phase} writes to QBO; it was already executed on 26 Sep 2026. "
                     "Pass --execute only with a fresh explicit chat yes.")
    EXECUTE = args.execute
    EVIDENCE = args.inputs_dir / "qbo_transfers_uf_readonly.json"
    DEPOSITS_CSV = args.inputs_dir / "proposed_deposits.csv"
    TARGET_COUNT, TARGET_SUM = args.target_count, args.target_sum
    OUT = resolve_out(args.out, TOOL)
    print(f"[INFO] Connecting company_a phase={args.phase}", flush=True)
    _, token_mgr, base, realm = connect()
    print("[INFO] Connected", flush=True)
    delete_ev = None
    deposit_ev = None
    tail_ev = None
    if args.phase in {"probe", "all"}:
        phase_probe(token_mgr, base, realm)
    if args.phase in {"delete", "all"}:
        delete_ev = phase_delete(token_mgr, base, realm)
    if args.phase in {"deposit", "all"}:
        deposit_ev = phase_deposit(token_mgr, base, realm)
    if args.phase == "tail":
        tail_ev = phase_tail(token_mgr, base, realm)
    if delete_ev or deposit_ev:
        write_report(delete_ev, deposit_ev)
    if tail_ev:
        extra = OUT / "UF_REVERSE_AND_ALLOCATE_REPORT.md"
        prior = extra.read_text(encoding="utf-8") if extra.exists() else ""
        block = [
            "",
            f"## Tail through last QBO Sales Receipt ({tail_ev.get('qbo_last_sales_receipt_date')})",
            "",
            f"- Sheet last filled: {tail_ev.get('sheet_last_filled')}",
            f"- Unlinked nonzero receipts: {tail_ev.get('unlinked_nonzero')}",
            f"- Deposits posted: {tail_ev.get('deposits_posted')} totaling ₦{tail_ev.get('deposited_sum')}",
            f"- UF after: ₦{tail_ev.get('uf_after')}",
            "",
        ]
        extra.write_text(prior.rstrip() + "\n" + "\n".join(block), encoding="utf-8")
        print(f"[INFO] appended tail to {extra}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
