"""Download an EPOS BookKeeping CSV for a date range WITHOUT posting anything, and print totals.

Wraps the pipeline downloader (view/export only; no split, transform or QBO upload):
  python -m code_scripts.epos_playwright --company company_a --from-date .. --to-date .. \\
      --output-dir <out> --output-filename <name>
then prints lines, gross (TOTAL Sales), net and cost per EPOS business day
(05:00-04:59 Lagos, --cutoff-hour) and writes business_day_totals.csv into --out.

Examples:
  python -m code_scripts.scripts.akponora_cutover.epos_sales_download --from 2026-09-25 --to 2026-09-30
  python -m code_scripts.scripts.akponora_cutover.epos_sales_download --summarize "<existing BookKeeping CSV>"
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from code_scripts.scripts.akponora_cutover._common import (
    BUSINESS_DAY_CUTOFF_HOUR, REPO_ROOT, business_date, parse_bookkeeping_ts, read_csv, resolve_out,
)

TOOL = "epos_sales_download"


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v or "0").replace(",", "").strip() or "0")
    except InvalidOperation:
        return Decimal(0)


def business_day_totals(rows, cutoff_hour: int = BUSINESS_DAY_CUTOFF_HOUR) -> dict[str, dict]:
    """{business_date: {lines, gross, net, cost}} from BookKeeping rows; 'Total:' rows ignored."""
    out: dict[str, dict] = defaultdict(lambda: {"lines": 0, "gross": Decimal(0), "net": Decimal(0), "cost": Decimal(0)})
    for r in rows:
        if " ".join(str(r.get("Staff") or "").split()) == "Total:":
            continue
        ts = parse_bookkeeping_ts(r.get("Date/Time"))
        if ts is None:
            continue
        d = out[business_date(ts, cutoff_hour).isoformat()]
        d["lines"] += 1
        d["gross"] += _dec(r.get("TOTAL Sales"))
        d["net"] += _dec(r.get("NET Sales"))
        d["cost"] += _dec(r.get("Cost Price"))
    return dict(sorted(out.items()))


def summarize(path: Path, cutoff_hour: int, out_dir: Path) -> dict[str, dict]:
    totals = business_day_totals(read_csv(path), cutoff_hour)
    out_csv = out_dir / "business_day_totals.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["business_date", "lines", "gross", "net", "cost"])
        for day, t in totals.items():
            w.writerow([day, t["lines"], t["gross"], t["net"], t["cost"]])
    print(f"{'business_date':<14}{'lines':>7}{'gross':>18}{'net':>18}{'cost':>18}")
    for day, t in totals.items():
        print(f"{day:<14}{t['lines']:>7}{t['gross']:>18.2f}{t['net']:>18.2f}{t['cost']:>18.2f}")
    tot = {k: sum((t[k] for t in totals.values()), Decimal(0)) for k in ("gross", "net", "cost")}
    print(f"{'TOTAL':<14}{sum(t['lines'] for t in totals.values()):>7}{tot['gross']:>18.2f}{tot['net']:>18.2f}{tot['cost']:>18.2f}")
    print("->", out_csv)
    return totals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", help="first date (YYYY-MM-DD)")
    ap.add_argument("--to", dest="end", help="last date (YYYY-MM-DD)")
    ap.add_argument("--summarize", type=Path, help="only summarise an existing BookKeeping CSV (no download)")
    ap.add_argument("--company", default="company_a")
    ap.add_argument("--cutoff-hour", type=int, default=BUSINESS_DAY_CUTOFF_HOUR)
    ap.add_argument("--out", type=Path, default=None, help="download folder (default outputs/epos_sales_download_<timestamp>/)")
    ap.add_argument("--filename", default=None, help="default BookKeeping_<from>_<to>.csv")
    a = ap.parse_args(argv)
    if a.summarize:
        summarize(a.summarize, a.cutoff_hour, resolve_out(a.out, TOOL))
        return 0
    if not (a.start and a.end):
        ap.error("--from and --to are required unless --summarize is given")
    date.fromisoformat(a.start)
    date.fromisoformat(a.end)
    out = resolve_out(a.out, TOOL)
    name = a.filename or f"BookKeeping_{a.start}_{a.end}.csv"
    env = dict(os.environ)
    env.setdefault("OIAT_COMPANIES_DIR", str(REPO_ROOT / "code_scripts" / "companies"))
    cmd = [sys.executable, "-m", "code_scripts.epos_playwright", "--company", a.company, "--from-date", a.start,
           "--to-date", a.end, "--output-dir", str(out), "--output-filename", name]
    print("running:", " ".join(cmd[1:]), flush=True)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, env=env)
    summarize(out / name, a.cutoff_hour, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
