"""Normalise captured EPOS stock adjustments into a durable read-only review queue.

Uses the existing stock_bridge reader's detail JSONL. Both stock-take and stock-
movement lists must be captured; incomplete captures are held, never called clean.
No QBO client, network calls, inventory writes, Slack or scheduler activation.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from code_scripts.akponora_ops.common import COMPANY, dump_json, read_csv
from code_scripts.akponora_ops.stock_reconciliation import decimal, digest
from code_scripts.akponora_ops.stock_snapshot import load_catalogue_file
from code_scripts.scripts.akponora_cutover.w7_create_items import norm_name, mult_of


def normalise(captures, catalogue, mapping_rows, *, from_date, through_date):
    """Capture dates are EPOS display dates; do not assume their UTC offset."""
    start, end = date.fromisoformat(from_date), date.fromisoformat(through_date)
    if start < date(2026, 10, 1) or end < start:
        raise ValueError("Choose an October-or-later capture window")
    names = defaultdict(list)
    for pid, p in catalogue.items():
        names[norm_name(p["Name"])].append((pid, p))
    approved = defaultdict(list)
    for r in mapping_rows:
        if r.get("Review Status", "").strip().casefold() == "approved":
            approved[str(r.get("EPOS Product ID", ""))].append(r)
    transfers, errors = {}, []
    for capture in captures:
        query = parse_qs(urlparse(capture.get("url", "")).query)
        tid = (query.get("TransferID") or [""])[0]
        if not tid.isdigit():
            errors.append("Adjustment detail is missing a stable TransferID")
            continue
        info = {r[0].strip(): r[1].strip() for r in capture.get("info", []) if len(r) == 2}
        try:
            when = datetime.strptime(info["Date"], "%d/%m/%Y %H:%M:%S")
        except (KeyError, ValueError):
            errors.append(f"Transfer {tid}: unreadable display date")
            continue
        if not start <= when.date() <= end:
            continue
        if info.get("Status", "").casefold() != "received":
            continue  # drafts do not change stock
        items = capture.get("items") or []
        if not items or [" ".join(str(c).upper().split()) for c in items[0]] != [
                "PRODUCT", "PRODUCT COST PRICE (EXC. TAX)", "QUANTITY VARIANCE", "VOLUME VARIANCE",
                "COST PRICE VARIANCE", "ITEM REASON"]:
            errors.append(f"Transfer {tid}: unexpected detail columns")
            continue
        body = {"info": info, "items": items[1:]}
        if tid in transfers and digest(transfers[tid]) != digest(body):
            errors.append(f"Transfer {tid}: conflicting captures; re-read the whole transfer")
            continue
        transfers[tid] = body
    events = []
    for tid, capture in sorted(transfers.items()):
        info = capture["info"]
        occurrences = Counter()
        for cells in capture["items"]:
            if len(cells) != 6:
                errors.append(f"Transfer {tid}: incomplete item detail")
                continue
            name, cost, full, loose, value, reason = cells
            candidates = names.get(norm_name(name), [])
            holds, pid, sku, iid, qty = [], "", "", "", None
            if len(candidates) != 1:
                holds.append("Product identity is missing or ambiguous; no name guessing or barcode matching.")
            else:
                pid, product = candidates[0]
                rules = approved.get(pid, [])
                if not product.get("IsStockTracked"):
                    holds.append("Adjustment is on a child or untracked product; verify its stock owner and units.")
                elif len(rules) != 1:
                    holds.append("Product has no unique approved mapping.")
                else:
                    rule = rules[0]
                    sku, iid = rule.get("Target QBO SKU", ""), rule.get("Target QBO Item Id", "")
                    if (rule.get("Target QBO Item Type") != "Inventory" or not sku.startswith("AKP-")
                            or sku.startswith("AKP-NS-") or not iid or iid == "15030"):
                        holds.append("Product does not target an approved new Inventory item.")
                    else:
                        try:
                            qty = decimal(full or "0") * mult_of(product) + decimal(loose or "0")
                        except ValueError:
                            holds.append("Adjustment quantity or canonical units could not be verified.")
            name_key = norm_name(name)
            occurrences[name_key] += 1
            # Transfer identity survives paging changes and appears only once across both logs.
            event_id = digest([COMPANY, tid, name_key, occurrences[name_key]])
            event = {"event_id": event_id, "transfer_id": tid, "occurred_at_epos": info["Date"],
                "location": info.get("Location", ""), "staff": info.get("Staff", ""),
                "note": info.get("Note", ""), "name": name, "epos_product_id": pid,
                "sku": sku, "qbo_item_id": iid, "canonical_qty_change": str(qty) if qty is not None else None,
                "epos_reason": reason, "source_cost": cost, "source_value_change": value, "holds": holds,
                "review_reason": ("EPOS stock addition needs a PO check. Is it a delivery or a count correction?"
                    if qty is not None and qty > 0 else "EPOS stock adjustment needs review."),
                "po_link_status": "UNVERIFIED", "financial_writes": False}
            event["evidence_sha256"] = digest(event)
            events.append(event)
    return {"schema_version": 1, "company": COMPANY, "from_date": from_date, "through_date": through_date,
        "date_basis": "EPOS displayed date; timezone not inferred", "events": events,
        "errors": sorted(set(errors)), "financial_writes": False}


def read_capture(folder, *, require_both=True):
    """Check every listed row has a detail capture; no page/row cursor is persisted."""
    details, errors = [], []
    for source in ("stocktakes", "stockmovements"):
        listing, detail = folder / f"epos_{source}_list.json", folder / f"epos_{source}_details.jsonl"
        if not listing.is_file():
            if require_both:
                errors.append(f"{source} list was not captured")
            continue
        listed = json.loads(listing.read_text())
        captured = [json.loads(line) for line in detail.read_text().splitlines() if line.strip()] if detail.is_file() else []
        done = {(r.get("_page"), r.get("_row")) for r in captured}
        missing = sum((r.get("page"), r.get("row")) not in done for r in listed)
        if missing:
            errors.append(f"{source}: {missing} listed adjustment(s) have no details")
        details.extend(captured)
    return details, errors


def collect(folder, from_date, through_date, *, reader=None):
    """Explicit view-only capture. Fresh directory avoids the old reader's page cursor."""
    start, end = date.fromisoformat(from_date), date.fromisoformat(through_date)
    if start < date(2026, 10, 1) or not 0 <= (end - start).days < 7:
        raise ValueError("Capture windows must be from October onward and at most seven days")
    if reader is None:
        from code_scripts.scripts.akponora_cutover.stock_bridge import scrape_adjustments
        reader = scrape_adjustments
    raw = folder / datetime.now(timezone.utc).strftime("capture_%Y%m%dT%H%M%S_%fZ")
    raw.mkdir(parents=True, exist_ok=False)
    reader(raw, from_date, "both", COMPANY)
    return raw


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--captures", type=Path, required=True)
    ap.add_argument("--capture-live", action="store_true", help="explicitly run the existing VIEW-ONLY EPOS reader into a fresh capture directory")
    ap.add_argument("--catalogue", type=Path, required=True)
    ap.add_argument("--mapping", type=Path, required=True)
    ap.add_argument("--from-date", required=True)
    ap.add_argument("--through-date", required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.capture_live:
        a.captures = collect(a.captures, a.from_date, a.through_date)
    captures, capture_errors = read_capture(a.captures)
    result = normalise(captures, load_catalogue_file(a.catalogue), read_csv(a.mapping),
        from_date=a.from_date, through_date=a.through_date)
    result["errors"] += capture_errors
    result["status"] = "INCOMPLETE" if result["errors"] else "REVIEW" if result["events"] else "NO_CAPTURED_ADJUSTMENTS"
    result["sources"] = {"captures": str(a.captures), "catalogue_sha256": digest(json.loads(a.catalogue.read_text())),
        "mapping_sha256": digest(read_csv(a.mapping))}
    result["evidence_sha256"] = digest(result)
    a.out.mkdir(parents=True, exist_ok=True)
    dump_json(a.out / "summary.json", result)
    print(json.dumps({"status": result["status"], "events": len(result["events"]), "errors": result["errors"]}))
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
