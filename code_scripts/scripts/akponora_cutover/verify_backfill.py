"""READ-ONLY check of company_a QBO SalesReceipts for a date range against EPOS controls.

For every business day in --from..--to it checks:
  * QBO has at least one SalesReceipt;
  * every SalesItemLine uses only allowed QBO item Ids
    (default {15030} = the pre-October catch-all; for October use --allowed-from-mapping
    and/or --allowed-items);
  * the QBO receipt total is within NGN 1 of the EPOS gross control;
  * no DocNumber appears twice on the day.

EPOS controls come from --controls (CSV with business_date, gross) or are computed
from an EPOS BookKeeping CSV (--bookkeeping): sum of 'TOTAL Sales' per business day,
where the business day runs 05:00-04:59 (--cutoff-hour).
QBO access is GET queries only (plus the normal OAuth refresh in token_manager).

Example:
  python -m code_scripts.scripts.akponora_cutover.verify_backfill --from 2026-09-16 --to 2026-09-24 \\
      --bookkeeping "<evidence-root>/As of 25th September/BookKeeping_2026_09_25_1245.csv"
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import (
    BUSINESS_DAY_CUTOFF_HOUR, COMPANY_KEY, ReadOnlyQBO, business_date, company_config, dump_json,
    parse_bookkeeping_ts, read_csv, resolve_out,
)

TOOL = "verify_backfill"
DEFAULT_ALLOWED = {"15030"}
TOLERANCE = Decimal("1")


def dec(v) -> Decimal:
    try:
        return Decimal(str(v or "0").replace(",", "").strip() or "0")
    except InvalidOperation:
        return Decimal(0)


def controls_from_csv(path: Path) -> dict[str, Decimal]:
    return {row["business_date"]: dec(row["gross"]) for row in read_csv(path)}


def controls_from_bookkeeping(path: Path, cutoff_hour: int = BUSINESS_DAY_CUTOFF_HOUR) -> dict[str, Decimal]:
    """EPOS gross (TOTAL Sales) per business day; 'Total:' footer rows are ignored."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for r in read_csv(path):
        if " ".join(str(r.get("Staff") or "").split()) == "Total:":
            continue
        ts = parse_bookkeeping_ts(r.get("Date/Time"))
        if ts is None:
            continue
        out[business_date(ts, cutoff_hour).isoformat()] += dec(r.get("TOTAL Sales"))
    return dict(out)


def summarise_receipts(receipts: list[dict]) -> dict:
    by_day = defaultdict(lambda: {"receipts": 0, "total": Decimal(0), "items": set(), "docnumbers": []})
    for s in receipts:
        d = by_day[s["TxnDate"]]
        d["receipts"] += 1
        d["total"] += Decimal(str(s["TotalAmt"]))
        d["docnumbers"].append(s.get("DocNumber"))
        for line in s.get("Line") or []:
            det = line.get("SalesItemLineDetail")
            if det:
                d["items"].add(det["ItemRef"]["value"])
    return by_day


def evaluate(by_day: dict, controls: dict[str, Decimal], allowed: set[str], start: str, end: str) -> tuple[bool, dict]:
    report, ok = {}, True
    days = sorted(d for d in set(controls) | set(by_day) if start <= d <= end)
    for day in days:
        d = by_day.get(day, {"receipts": 0, "total": Decimal(0), "items": set(), "docnumbers": []})
        dup = len(d["docnumbers"]) - len(set(d["docnumbers"]))
        ctrl = controls.get(day)
        diff = d["total"] - (ctrl or Decimal(0))
        bad_items = sorted(d["items"] - allowed)
        day_ok = ctrl is not None and abs(diff) < TOLERANCE and not bad_items and dup == 0 and d["receipts"] > 0
        ok &= day_ok
        report[day] = {
            "receipts": d["receipts"], "qbo_total": str(d["total"]),
            "epos_control_gross": None if ctrl is None else str(ctrl), "diff": str(diff),
            "item_ids": sorted(d["items"]), "disallowed_item_ids": bad_items,
            "duplicate_docnumbers": dup, "ok": day_ok,
        }
        print(day, "OK " if day_ok else "BAD", d["receipts"], d["total"], "diff", diff,
              f"items={len(d['items'])}", f"disallowed={bad_items[:5]}", "dups", dup)
    return ok and bool(days), report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", required=True, help="first TxnDate / business date (YYYY-MM-DD)")
    ap.add_argument("--to", dest="end", required=True, help="last TxnDate / business date (YYYY-MM-DD)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--controls", type=Path, help="CSV with business_date,gross")
    src.add_argument("--bookkeeping", type=Path, help="EPOS BookKeeping CSV to compute controls from")
    ap.add_argument("--cutoff-hour", type=int, default=BUSINESS_DAY_CUTOFF_HOUR)
    ap.add_argument("--allowed-items", default="",
                    help="comma-separated QBO item Ids allowed on lines (default 15030 unless --allowed-from-mapping)")
    ap.add_argument("--allowed-from-mapping", action="store_true",
                    help="allow every Target QBO Item Id in the installed company_a product-conversion mapping")
    ap.add_argument("--mapping", type=Path, default=None,
                    help="explicit mapping CSV for --allowed-from-mapping (default: the installed company_a file)")
    ap.add_argument("--company", default=COMPANY_KEY)
    ap.add_argument("--out", type=Path, default=None, help="output folder (default outputs/verify_backfill_<timestamp>/)")
    a = ap.parse_args(argv)
    for v in (a.start, a.end):
        date.fromisoformat(v)

    allowed = {x.strip() for x in a.allowed_items.split(",") if x.strip()}
    if a.allowed_from_mapping:
        if a.mapping:
            ids = {r.get("Target QBO Item Id", "").strip() for r in read_csv(a.mapping)}
        else:
            ids = set(company_config(a.company).product_conversion_approved_item_ids)
        ids.discard("")
        if not ids:
            raise SystemExit("mapping has no Target QBO Item Ids")
        allowed |= ids
    if not allowed:
        allowed = set(DEFAULT_ALLOWED)

    controls = controls_from_csv(a.controls) if a.controls else controls_from_bookkeeping(a.bookkeeping, a.cutoff_hour)
    qbo = ReadOnlyQBO(a.company)
    receipts = qbo.query_all(
        f"select * from SalesReceipt where TxnDate >= '{a.start}' and TxnDate <= '{a.end}'", "SalesReceipt")
    ok, report = evaluate(summarise_receipts(receipts), controls, allowed, a.start, a.end)
    out = resolve_out(a.out, TOOL)
    path = out / f"verification_{a.start}_{a.end}.json"
    dump_json(path, {"all_ok": ok, "from": a.start, "to": a.end, "receipts": len(receipts),
                     "controls_source": str(a.controls or a.bookkeeping), "cutoff_hour": a.cutoff_hour,
                     "allowed_item_ids_count": len(allowed),
                     "allowed_item_ids": sorted(allowed) if len(allowed) <= 50 else "(from mapping)",
                     "days": report})
    print("ALL OK" if ok else "PROBLEMS FOUND", "->", path)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
