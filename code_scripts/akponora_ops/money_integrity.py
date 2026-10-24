"""Read-only cross-process money evidence. Never invent a count or record a payment."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from code_scripts.akponora_ops.work_state import digest


def repayment_days(rows, *, parse_day, parse_money):
    days = {}
    for row in rows[1:]:
        row = list(row) + [""] * 11
        if not any(str(x).strip() for x in row[:7]):
            continue
        day, amount = parse_day(row[2]), parse_money(row[1])
        if not day or amount is None or not amount.is_finite() or amount <= 0:
            continue
        record = days.setdefault(day, {"amount": Decimal(0), "rows": []})
        record["amount"] += amount
        record["rows"].append({"id": row[10] or digest(row[:7])[:20], "amount": str(amount), "account": row[3]})
    return {day: {**record, "source_sha": digest(record["rows"])} for day, record in days.items()}


def overage_overlap(day, overage, repayments):
    r = repayments.get(day) or {}
    amount = Decimal(str(r.get("amount") or 0))
    if overage <= 0 or amount <= 0:
        return None
    return {"day": day, "repayments": str(amount), "proposed_overage": str(overage),
            "source_sha": r.get("source_sha"), "owner": "OIAT / owner",
            "reason": "till money above sales may include customer repayments; confirm sales-only allocation before banking"}


def check_cash_count(record):
    """Explicit physical cash evidence; missing inputs stay unavailable, never zero-filled."""
    required = ("opening_float", "cash_sales", "cash_repayments", "supplier_cash_payments", "cash_banked",
                "other_approved_outflows", "closing_count")
    if not record.get("date") or not record.get("counted_by") or any(record.get(k) in (None, "") for k in required):
        return {"status": "needs evidence", "reason": "date, counted-by and every cash movement/count are required"}
    try:
        n = {k: Decimal(str(record[k]).replace(",", "")) for k in required}
        if any(not x.is_finite() or x < 0 for x in n.values()):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return {"status": "needs evidence", "reason": "cash counts and movements must be finite non-negative numbers"}
    expected = n["opening_float"] + n["cash_sales"] + n["cash_repayments"] - n["supplier_cash_payments"] - n["cash_banked"] - n["other_approved_outflows"]
    difference = n["closing_count"] - expected
    return {"date": record["date"], "counted_by": record["counted_by"], "expected_cash": str(expected),
            "counted_cash": str(n["closing_count"]), "difference": str(difference),
            "status": "matches" if difference == 0 else "review", "evidence_sha": digest(record)}


def check_bank_statement(record):
    if any(record.get(k) in (None, "") for k in ("date", "account_id", "statement_balance", "book_balance", "statement_reference", "checked_by", "verified_outstanding_net")):
        return {"status": "needs evidence", "reason": "statement, account, date, balance and checker evidence required"}
    try:
        book, bank = Decimal(str(record["book_balance"])), Decimal(str(record["statement_balance"]))
        pending = Decimal(str(record.get("verified_outstanding_net", "0")))
        if not all(x.is_finite() for x in (book, bank, pending)):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return {"status": "needs evidence", "reason": "bank evidence contains invalid balances"}
    difference = bank + pending - book
    return {"date": record["date"], "account_id": record["account_id"], "difference": str(difference),
            "status": "matches" if difference == 0 else "review", "evidence_sha": digest(record)}


def evidence_report(root, day):
    import json
    from pathlib import Path
    path = Path(root) / 'ops/company_a/money_evidence' / (day + '.json')
    record = json.loads(path.read_text()) if path.exists() else {}
    cash = record.get('cash') or {}
    banks = record.get('banks') or []
    if cash.get('date') not in (None, day) or any(b.get('date') != day for b in banks):
        return {'status': 'review', 'reason': 'cash/bank evidence belongs to a different business day'}
    return {'business_date': day, 'cash': check_cash_count(cash),
            'banks': [check_bank_statement(b) for b in banks] or [{'status': 'needs evidence', 'reason': 'no bank statement supplied'}],
            'scope': 'checks supplied evidence arithmetic; actual counts and statement authentication need human signoff'}


def review_register(root):
    import sqlite3
    from pathlib import Path
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    records = []
    for path in (Path(root) / 'ops/company_a').glob('*/work.sqlite3'):
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            rows = db.execute("SELECT namespace,identity,status,first_seen FROM work WHERE namespace IN "
                              "('credit','payment','stock','po_accounting') AND status NOT IN ('verified','posted','already posted')").fetchall()
        for namespace, identity, status, first in rows:
            records.append({'process': namespace, 'identity': identity, 'status': status, 'first_seen': first,
                            'age_days': max(0, (now - datetime.fromisoformat(first)).days), 'owner': 'OIAT / owner'})
    return sorted(records, key=lambda r: (r['first_seen'], r['process'], r['identity']))
