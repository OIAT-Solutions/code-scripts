"""Evidence-only stock investigation. Never contacts QBO or posts adjustments.

``investigate`` joins a saved stock snapshot to the executed opening register,
opening exclusions and bill reviews. Multiple explanations are retained, not
forced into a single cause. ``count-plan`` builds a draft from verified counts;
it cannot post. A live FIFO/valuation-tested posting adapter is a separate gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from code_scripts.akponora_ops.common import COMPANY, dump_json, read_csv, write_csv


def decimal(value):
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid quantity: {value!r}") from None
    if not number.is_finite():
        raise ValueError("Quantity must be finite")
    return number


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unique(rows, field):
    indexed = {}
    for row in rows:
        key = str(row.get(field) or "").strip()
        if not key or key in indexed:
            raise ValueError(f"Missing or duplicate {field}: {key}")
        indexed[key] = row
    return indexed


def investigate(snapshot, register, exclusions, bills=(), bill_lines=(), movements=None):
    if snapshot.get("company") != COMPANY:
        raise ValueError("Only company_a stock evidence is supported")
    opening = unique(register, "Target QBO SKU")
    excluded = unique(exclusions, "QBO SKU")
    po_status = unique(bills, "PO")
    movements = movements or {}
    last_posted = (snapshot.get("business_context") or {}).get("last_posted_sales_date")
    adjustment_by_sku = {}
    for event in movements.get("events", []):
        # EPOS display dates only: no guessed timezone or same-day alignment.
        displayed_day = datetime.strptime(event["occurred_at_epos"], "%d/%m/%Y %H:%M:%S").date().isoformat()
        if (last_posted and displayed_day <= last_posted and not event.get("holds")
                and event.get("canonical_qty_change") is not None):
            adjustment_by_sku.setdefault(event["sku"], []).append(event)
    # An absent PO status is not evidence that a purchase posted.
    pending = {}
    for line in bill_lines:
        po = str(line.get("PO") or "")
        status = po_status.get(po, {}).get("Status", "UNKNOWN")
        if status in {"POSTED", "ALREADY_POSTED", "ADOPTED", "RESOLVED"}:
            continue
        iid = str(line.get("QBO Item Id") or "")
        if iid:
            pending.setdefault(iid, []).append({"po": po, "status": status,
                "qty": str(decimal(line["QBO Qty"])), "reason": po_status.get(po, {}).get("Reasons", "")})
    result = []
    for row in snapshot.get("rows", []):
        if row.get("type") != "Inventory":
            continue
        sku = row.get("family_sku", "")
        o, e = opening.get(sku), excluded.get(sku)
        q, ep, delta = (row.get(k) for k in ("qbo_qty_on_hand", "epos_qty_canonical", "difference"))
        if delta == 0 and (q is None or decimal(q) >= 0) and not e:
            continue
        clues = []
        opening_qty = o.get("QtyOnHand") if o else None
        if not o or str(o.get("QBO Item Id")) != str(row.get("qbo_item_id")):
            clues.append("OPENING_IDENTITY_NOT_VERIFIED")
        if opening_qty is not None and decimal(opening_qty) == 0:
            clues.append("OPENED_AT_ZERO")
        excluded_qty = e.get("Qty excluded (canonical units)") if e else None
        if e:
            clues.append("DELIBERATELY_EXCLUDED_OPENING_REQUIRES_EVIDENCE")
            if delta is not None and decimal(delta) == decimal(excluded_qty):
                clues.append("GAP_EQUALS_EXCLUDED_OPENING")
        receipts = pending.get(str(row.get("qbo_item_id")), [])
        if receipts:
            clues.append("UNPOSTED_PURCHASE_CANDIDATE")
        adjustments = adjustment_by_sku.get(sku, [])
        adjustment_qty = sum((decimal(e["canonical_qty_change"]) for e in adjustments), Decimal(0))
        if adjustments:
            clues.append("CAPTURED_EPOS_ADJUSTMENTS_REQUIRE_CLASSIFICATION")
            if delta is not None and decimal(delta) == adjustment_qty:
                clues.append("GAP_EQUALS_CAPTURED_ADJUSTMENTS_THROUGH_POSTED_DAY")
        if row.get("likely_timing"):
            clues.append("TIMING_HINT_NOT_QUANTITY_PROOF")
        if q is not None and decimal(q) < 0:
            clues.append("NEGATIVE_QBO")
        if ep is not None and decimal(ep) < 0:
            clues.append("NEGATIVE_EPOS_REQUIRES_COUNT")
        if delta == 0 and q is not None and decimal(q) < 0:
            clues.append("BOTH_SYSTEMS_AGREE_BUT_NEGATIVE")
        result.append({"sku": sku, "qbo_item_id": row.get("qbo_item_id"), "name": row.get("qbo_name"),
            "status": row.get("status"), "qbo_qty": q, "epos_qty": ep, "difference": delta,
            "opening_qty": opening_qty, "excluded_opening_qty": excluded_qty,
            "epos_adjustment_qty_through_posted_day": str(adjustment_qty),
            "epos_adjustment_refs": [e["transfer_id"] for e in adjustments],
            "pending_purchases": receipts, "clues": clues,
            "next_step": "Review purchase and stock-movement history at a common cutoff; no adjustment authorised."})
    result.sort(key=lambda r: ("NEGATIVE_QBO" not in r["clues"], r["sku"]))
    counts = Counter(c for r in result for c in r["clues"])
    body = {"schema_version": 1, "company": COMPANY, "snapshot_at": snapshot.get("generated_at"),
        "business_context": snapshot.get("business_context"), "sources": snapshot.get("sources"),
        "summary": dict(counts), "rows": result, "financial_writes": False}
    body["movement_capture_errors"] = movements.get("errors", [])
    body["cutoff_warning"] = "EPOS is live and QBO lags; matching quantities are investigation clues, not posting authority."
    body["evidence_sha256"] = digest(body)
    return body


def count_plan(snapshot, decisions, *, cutoff, offset_account_id, approval_basis):
    """Draft only: live EPOS stock and a timing hint can never authorise a count."""
    day = date.fromisoformat(cutoff)
    if day < date(2026, 10, 1) or snapshot.get("company") != COMPANY:
        raise ValueError("Only new Company A inventory from 1 October is supported")
    if not offset_account_id or not approval_basis.strip():
        raise ValueError("Accountant's offset account and accounting basis are required")
    rows = unique(snapshot.get("rows", []), "family_sku")
    unique(decisions, "sku")
    entries = []
    for d in decisions:
        row = rows.get(d["sku"])
        if (not row or row.get("type") != "Inventory" or not row["family_sku"].startswith("AKP-")
                or row["family_sku"].startswith("AKP-NS-") or row.get("qbo_active") is not True
                or not row.get("qbo_item_id") or str(row.get("qbo_item_id")) == "15030" or row.get("flags")):
            raise ValueError(f"Not a verified active new inventory item: {d['sku']}")
        required = ("count_source", "confirmed_by", "reason", "transactions_reconciled_by")
        if any(not str(d.get(k) or "").strip() for k in required) or d.get("cutoff") != cutoff:
            raise ValueError(f"Count evidence and reconciled transaction cutoff required: {d['sku']}")
        target, baseline = decimal(d["verified_qty"]), decimal(d["qbo_qty_at_cutoff"])
        if target < 0:
            raise ValueError("A negative EPOS count cannot be used as a physical target")
        entries.append({"sku": d["sku"], "qbo_item_id": row["qbo_item_id"], "cutoff": cutoff,
            "quantity_at_cutoff": str(baseline), "verified_quantity": str(target),
            "quantity_change": str(target - baseline), "evidence": dict(d)})
    body = {"schema_version": 1, "company": COMPANY, "kind": "count_variance_draft", "cutoff": cutoff,
        "offset_account_id": str(offset_account_id), "accounting_basis": approval_basis,
        "source_snapshot_sha256": digest(snapshot), "entries": sorted(entries, key=lambda e: e["sku"]),
        "postable": False, "holds": ["Live posting and FIFO valuation adapter has not been validated.",
            "Specific production approval and fresh transaction, mapping and quantity checks are required."],
        "financial_writes": False}
    body["plan_sha256"] = digest(body)
    return body


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("investigate")
    p.add_argument("--snapshot", required=True, type=Path)
    p.add_argument("--opening-register", required=True, type=Path)
    p.add_argument("--opening-exclusions", required=True, type=Path)
    p.add_argument("--bills-review", type=Path)
    p.add_argument("--bill-lines", type=Path)
    p.add_argument("--movements", type=Path)
    p.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("count-plan")
    p.add_argument("--snapshot", required=True, type=Path)
    p.add_argument("--decisions", required=True, type=Path)
    p.add_argument("--cutoff", required=True)
    p.add_argument("--offset-account-id", required=True)
    p.add_argument("--accounting-basis", required=True)
    p.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    snapshot = json.loads(a.snapshot.read_text())
    if a.command == "investigate":
        result = investigate(snapshot, read_csv(a.opening_register), read_csv(a.opening_exclusions),
            read_csv(a.bills_review) if a.bills_review else [], read_csv(a.bill_lines) if a.bill_lines else [],
            json.loads(a.movements.read_text()) if a.movements else None)
        a.out.mkdir(parents=True, exist_ok=True)
        dump_json(a.out / "investigation.json", result)
        flat = [{**r, "clues": "; ".join(r["clues"]), "pending_purchases": json.dumps(r["pending_purchases"])} for r in result["rows"]]
        if flat:
            write_csv(a.out / "review.csv", flat, list(flat[0]))
    else:
        result = count_plan(snapshot, json.loads(a.decisions.read_text()), cutoff=a.cutoff,
            offset_account_id=a.offset_account_id, approval_basis=a.accounting_basis)
        a.out.mkdir(parents=True, exist_ok=True)
        dump_json(a.out / "plan.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"rows", "entries", "sources"}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
